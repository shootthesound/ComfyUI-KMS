"""KSampler Multi-Choice — a KSampler that shows you N seeds before committing.

It probes the first `probe_steps` steps for each of `num_seeds` consecutive seeds,
PAUSES with the candidate scenes shown as clickable thumbnails on the node, then
finishes ONLY the seed you click — continuing that trajectory from its probe
endpoint, so no probe work is wasted and the result is exactly what a normal run
of that seed would produce (deterministic samplers).

Model agnostic: the continuation is the same split-sigma convention two-stage
sampler workflows rely on (zero-noise resume), which is exact for both classic
EPS models and flow/CONST models. On distilled models (turbo/TDM/Lightning/etc.)
the composition is committed after 1-2 steps, so probing is nearly free; on
classic multi-step models raise probe_steps (~20-30% of the schedule) so the
previews are readable.

Standard KSampler-style inputs — guider, noise, sampler and sigmas are built
internally (an optional SIGMAS input overrides the schedule). Set timeout_sec if
the workflow may run unattended; while waiting, Cancel aborts the run.

After a run completes the thumbnails stay clickable: the probe endpoints are kept
in a server-side cache, so clicking another candidate re-queues the workflow and
the node finishes that seed directly — no re-probe, no waiting.
"""
import threading

import numpy as np
import torch
import torch.nn.functional as F

# node_id -> {"event": Event, "pick": int} for nodes blocked awaiting a click
_PENDING = {}

# node_id -> cached probe results from the last run, so candidates can still be
# rendered after the run finished: {"sig", "endpoints", "preview_sheet", "images",
# "base_seed", "n_probe", "queued_picks", "done"}
_CACHE = {}

try:
    from server import PromptServer
    from aiohttp import web

    @PromptServer.instance.routes.post("/multichoice/pick")
    async def _multichoice_pick(request):
        data = await request.json()
        nid = str(data.get("node_id"))
        pick = int(data.get("pick", 0))
        p = _PENDING.get(nid)
        if p is not None:
            # Node is blocked mid-execution — unblock it with this pick.
            p["pick"] = pick
            p["event"].set()
            return web.json_response({"ok": True, "mode": "live"})
        c = _CACHE.get(nid)
        if c is not None:
            # Run already finished — queue the pick; the frontend re-queues the
            # prompt and run() will serve it from the cached probe endpoints.
            # A list so rapid multiple clicks each get their own queued run.
            c["queued_picks"].append(pick)
            return web.json_response({"ok": True, "mode": "queued"})
        return web.json_response({"ok": False})
except Exception:
    PromptServer = None


def _cat_pad(imgs):
    """torch.cat a list of [1,H,W,C] previews along batch, zero-padding to common size."""
    mh = max(i.shape[1] for i in imgs)
    mw = max(i.shape[2] for i in imgs)
    out = []
    for i in imgs:
        ph, pw = mh - i.shape[1], mw - i.shape[2]
        out.append(F.pad(i.movedim(-1, 1), (0, pw, 0, ph)).movedim(1, -1))
    return torch.cat(out, dim=0)


def _label_index(img_1hwc, idx, seed):
    """Stamp 'index · seed N' in the top-left corner of a [1,H,W,C] image."""
    from PIL import Image, ImageDraw
    arr = (img_1hwc[0, :, :, :3].clamp(0, 1).cpu().numpy() * 255).astype(np.uint8)
    pil = Image.fromarray(arr).convert("RGB")
    draw = ImageDraw.Draw(pil)
    text = f"{idx} · seed {seed}"
    s = max(12, pil.height // 12)
    try:
        from PIL import ImageFont
        font = ImageFont.load_default(size=s)
    except Exception:
        font = None
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
        draw.text((10 + dx, 6 + dy), text, fill=(0, 0, 0), font=font)
    draw.text((10, 6), text, fill=(0, 255, 0), font=font)
    return torch.from_numpy(np.array(pil).astype(np.float32) / 255.0).unsqueeze(0)


class _SeededNoise:
    """Same behaviour as the RandomNoise node's output object."""
    def __init__(self, seed):
        self.seed = int(seed)

    def generate_noise(self, input_latent):
        import comfy.sample
        return comfy.sample.prepare_noise(input_latent["samples"], self.seed,
                                          input_latent.get("batch_index", None))


class _ZeroNoise:
    """DisableNoise equivalent — for continuing a trajectory that is already noised
    (split-sigma continuation)."""
    def __init__(self, seed):
        self.seed = int(seed)

    def generate_noise(self, input_latent):
        s = input_latent["samples"]
        return torch.zeros(s.shape, dtype=s.dtype, layout=s.layout, device="cpu")


def _make_guider(model, positive, negative, cfg):
    """Same construction as the core CFGGuider node. cfg=1.0 skips the negative pass
    (ComfyUI's built-in cfg1 optimization)."""
    import comfy.samplers
    guider = comfy.samplers.CFGGuider(model)
    guider.set_conds(positive, negative)
    guider.set_cfg(cfg)
    return guider


def _make_sampler(sampler_name):
    import comfy.samplers
    return comfy.samplers.sampler_object(sampler_name)


def _resolve_sigmas(guider, scheduler, steps, denoise, sigmas):
    """The schedule to sample with: the SIGMAS override if connected, else the model's
    standard schedule (identical to BasicScheduler's output for this denoise).
    denoise < 1.0 keeps only the tail of a longer schedule — the usual img2img
    partial-denoise convention."""
    import comfy.samplers
    if sigmas is not None:
        return sigmas
    steps = int(steps)
    total_steps = steps
    if denoise < 1.0:
        if denoise <= 0.0:
            raise ValueError("KSampler Multi-Choice: denoise must be > 0")
        total_steps = int(steps / denoise)
    print(f"[MultiChoice] using the model's standard schedule "
          f"('{scheduler}', {steps} steps, denoise {denoise})")
    ms = guider.model_patcher.get_model_object("model_sampling")
    s = comfy.samplers.calculate_sigmas(ms, scheduler, total_steps).cpu()
    return s[-(steps + 1):]


def _to_data_url(img_1hwc, max_side=384):
    """Downscale a [1,H,W,C] preview and encode it as a JPEG data URL for the frontend."""
    import base64
    import io
    from PIL import Image
    arr = (img_1hwc[0, :, :, :3].clamp(0, 1).cpu().numpy() * 255).astype(np.uint8)
    pil = Image.fromarray(arr)
    pil.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _cond_sig(conds):
    """Cheap fingerprint of a CONDITIONING list — shape plus tensor statistics."""
    out = []
    for c in conds:
        t = c[0]
        out.append((tuple(t.shape), float(t.float().mean()), float(t.float().std())))
    return tuple(out)


def _probe_sig(model, positive, negative, latent_image, cfg, sampler_name, num_seeds,
               n_probe, sigmas):
    """Fingerprint of everything the cached probe endpoints depend on. The base seed
    is deliberately NOT included: clicking a thumbnail means 'finish THAT candidate',
    even if the seed widget has since incremented/randomized (control_after_generate)."""
    s = latent_image["samples"]
    return (id(model), _cond_sig(positive), _cond_sig(negative),
            tuple(s.shape), float(s.float().mean()), float(s.float().std()),
            float(cfg), str(sampler_name), int(num_seeds), int(n_probe),
            tuple(round(float(x), 6) for x in sigmas.flatten().tolist()))


def _send_event(name, payload):
    if PromptServer is not None:
        try:
            PromptServer.instance.send_sync(name, payload)
        except Exception:
            pass


def _await_pick(node_id, images, timeout_sec):
    """Push the candidate previews to the node's UI and block the execution thread
    until the user clicks one (or interrupt/timeout). Returns the picked index."""
    import comfy.model_management
    if PromptServer is None or node_id is None:
        print("[MultiChoice] no UI server available — auto-picking candidate 0")
        return 0
    nid = str(node_id)
    ev = threading.Event()
    _PENDING[nid] = {"event": ev, "pick": 0}
    PromptServer.instance.send_sync("multichoice.candidates",
                                    {"node_id": nid, "images": images, "waiting": True})
    print(f"[MultiChoice] waiting — click a candidate on the KSampler Multi-Choice node "
          f"(node {nid})")
    waited = 0.0
    try:
        while not ev.wait(0.2):
            comfy.model_management.throw_exception_if_processing_interrupted()
            waited += 0.2
            if timeout_sec and waited >= timeout_sec:
                print(f"[MultiChoice] pick timed out after {int(timeout_sec)}s — using candidate 0")
                break
        return int(_PENDING[nid]["pick"])
    finally:
        _PENDING.pop(nid, None)
        try:
            PromptServer.instance.send_sync("multichoice.done", {"node_id": nid})
        except Exception:
            pass


def _finish_sample(guider, sampler, noise, latent_dict, run_sigmas):
    """Run `run_sigmas` on latent_dict with the given noise and return the
    (output, denoised_output) LATENT pair, like SamplerCustomAdvanced."""
    import comfy.model_management
    import comfy.sample
    import comfy.utils
    import latent_preview

    latent = latent_dict.copy()
    samples_in = comfy.sample.fix_empty_latent_channels(
        guider.model_patcher, latent["samples"],
        latent.get("downscale_ratio_spacial", None), latent.get("downscale_ratio_temporal", None))
    latent["samples"] = samples_in
    noise_mask = latent.get("noise_mask", None)

    x0_output = {}
    callback = latent_preview.prepare_callback(guider.model_patcher, run_sigmas.shape[-1] - 1, x0_output)
    disable_pbar = not comfy.utils.PROGRESS_BAR_ENABLED
    samples = guider.sample(noise.generate_noise(latent), samples_in, sampler, run_sigmas,
                            denoise_mask=noise_mask, callback=callback, disable_pbar=disable_pbar,
                            seed=noise.seed)
    samples = samples.to(comfy.model_management.intermediate_device())

    out = latent.copy()
    out.pop("downscale_ratio_spacial", None)
    out.pop("downscale_ratio_temporal", None)
    out["samples"] = samples
    if "x0" in x0_output:
        out_denoised = out.copy()
        out_denoised["samples"] = guider.model_patcher.model.process_latent_out(x0_output["x0"].cpu())
    else:
        out_denoised = out
    return out, out_denoised


def _decode_preview(vae, samples):
    """Decode a candidate latent to a [1,H,W,C] image. Video-format latents may carry
    a length-1 temporal axis — take the first frame."""
    v = samples
    if v.dim() == 5:
        v = v[:, :, :1]
    img = vae.decode(v)
    if img.dim() == 5:
        img = img.reshape(-1, img.shape[-3], img.shape[-2], img.shape[-1])
    return img[:1]


class KSamplerMultiChoice:
    @classmethod
    def INPUT_TYPES(cls):
        import comfy.samplers
        return {"required": {
            "model": ("MODEL",),
            "positive": ("CONDITIONING",),
            "negative": ("CONDITIONING",),
            "latent_image": ("LATENT",),
            "vae": ("VAE", {"tooltip": "VAE matching the model — used to decode the clickable "
                                       "candidate previews (a fast preview VAE like taesd/taew "
                                       "also works)."}),
            "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff,
                             "control_after_generate": True,
                             "tooltip": "Base seed: seeds seed, seed+1, ... seed+num_seeds-1 "
                                        "are probed."}),
            "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 100.0, "step": 0.1,
                "tooltip": "1.0 for distilled / CFG-free models (the negative pass is skipped); "
                           "otherwise your model's usual CFG (e.g. SDXL ~7, flow models ~3-5)."}),
            "sampler_name": (comfy.samplers.KSampler.SAMPLERS, {"default": "euler",
                "tooltip": "Deterministic samplers (euler, etc.) make the finished image exactly "
                           "match a normal run of the picked seed."}),
            "scheduler": (comfy.samplers.KSampler.SCHEDULERS, {"default": "simple",
                "tooltip": "Scheduler for the schedule (derived from the model, same as "
                           "BasicScheduler) — use what you'd normally pick for this model. "
                           "Ignored when a SIGMAS override is connected."}),
            "steps": ("INT", {"default": 8, "min": 1, "max": 100,
                "tooltip": "Full schedule length — the model's normal step count (e.g. 8 for "
                           "turbo/distilled models, 20-30 for classic ones)."}),
            "num_seeds": ("INT", {"default": 8, "min": 2, "max": 64,
                "tooltip": "How many consecutive seeds to browse — one thumbnail each. "
                           "Cost is num_seeds x probe_steps model steps."}),
            "probe_steps": ("INT", {"default": 2, "min": 1, "max": 16,
                "tooltip": "Steps run per seed before capturing its preview. 1-2 is enough on "
                           "distilled models; on classic multi-step models use ~20-30% of steps "
                           "so the composition is readable. The picked seed continues from this "
                           "point, so probe work is never wasted."}),
            "timeout_sec": ("INT", {"default": 0, "min": 0, "max": 3600,
                "tooltip": "Auto-pick candidate 0 after this many seconds without a click. "
                           "0 = wait forever (Cancel/interrupt still works). Set a value if this "
                           "workflow may run unattended/headless."}),
            "denoise": ("FLOAT", {"default": 1.0, "min": 0.01, "max": 1.0, "step": 0.01,
                "tooltip": "1.0 = txt2img (full schedule). Lower it to browse seeds for an "
                           "img2img/refine pass — only the last denoise fraction of a longer "
                           "schedule runs on your input latent, exactly like a standard "
                           "KSampler. Ignored when a SIGMAS override is connected."}),
        }, "optional": {
            "sigmas": ("SIGMAS", {"tooltip": "Optional custom schedule, used instead of the "
                                             "standard one from scheduler/steps."}),
        }, "hidden": {"unique_id": "UNIQUE_ID"}}

    RETURN_TYPES = ("LATENT", "LATENT", "IMAGE", "STRING")
    RETURN_NAMES = ("output", "denoised_output", "previews", "info")
    FUNCTION = "run"
    CATEGORY = "sampling"
    DESCRIPTION = ("A KSampler that shows you N seeds before committing: probes num_seeds "
                   "consecutive seeds for probe_steps steps each, PAUSES with the candidate "
                   "scenes as clickable thumbnails on this node, then finishes ONLY the seed "
                   "you click — continuing from its probe endpoint, so the result is exactly "
                   "what that seed produces for less than the cost of one extra full render. "
                   "After the run the thumbnails stay clickable — pick another candidate and "
                   "it renders too, straight from its cached probe endpoint. Works with any "
                   "model. Cancel the run to abort while it waits; set timeout_sec for "
                   "unattended runs.")

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # The result depends on the interactive click, which is not an input —
        # never serve this node from cache.
        return float("nan")

    def run(self, model, positive, negative, latent_image, vae, seed, cfg, sampler_name,
            scheduler, steps, num_seeds=8, probe_steps=2, timeout_sec=0, denoise=1.0,
            sigmas=None, unique_id=None):
        import comfy.model_management
        import comfy.sample
        import comfy.utils

        guider = _make_guider(model, positive, negative, cfg)
        sampler = _make_sampler(sampler_name)
        sigmas = _resolve_sigmas(guider, scheduler, steps, denoise, sigmas)

        n_probe = min(int(probe_steps), sigmas.shape[-1] - 1)
        probe_sigmas = sigmas[:n_probe + 1]

        latent = latent_image.copy()
        samples_in = comfy.sample.fix_empty_latent_channels(
            guider.model_patcher, latent_image["samples"],
            latent.get("downscale_ratio_spacial", None), latent.get("downscale_ratio_temporal", None))
        latent["samples"] = samples_in
        noise_mask = latent.get("noise_mask", None)
        batch_inds = latent.get("batch_index", None)

        nid = str(unique_id) if unique_id is not None else None
        sig = _probe_sig(model, positive, negative, latent, cfg, sampler_name,
                         num_seeds, n_probe, sigmas)
        cache = _CACHE.get(nid) if nid else None
        if cache is not None and cache["sig"] != sig:
            # Inputs changed — the cached candidates are stale.
            _CACHE.pop(nid, None)
            cache = None

        if cache is not None and cache["queued_picks"]:
            # A thumbnail was clicked after the previous run finished — serve it
            # straight from the cached probe endpoints: no probe pass, no waiting.
            endpoints = cache["endpoints"]
            preview_sheet = cache["preview_sheet"]
            base_seed = cache["base_seed"]
            pick = min(max(0, int(cache["queued_picks"].pop(0))), len(endpoints) - 1)
            print(f"[MultiChoice] rendering cached candidate {pick} (no re-probe)")
            # Repopulate the grid in case the page was reloaded since the probe run.
            _send_event("multichoice.candidates",
                        {"node_id": nid, "images": cache["images"], "waiting": False})
        else:
            base_seed = int(seed)
            pbar = comfy.utils.ProgressBar(int(num_seeds) * n_probe)

            # Probe pass: for each seed keep the x0 preview and the raw trajectory
            # endpoint the continuation resumes from.
            endpoints, previews = [], []
            for i in range(int(num_seeds)):
                seed_i = base_seed + i
                noise_i = comfy.sample.prepare_noise(samples_in, seed_i, batch_inds)

                captured = []

                def cb(step, x0, x, total_steps, _i=i):
                    captured.append(x0)
                    pbar.update_absolute(_i * n_probe + step + 1)

                probe_out = guider.sample(noise_i, samples_in, sampler, probe_sigmas,
                                          denoise_mask=noise_mask, callback=cb, disable_pbar=True,
                                          seed=seed_i)
                if not captured:
                    continue
                endpoints.append(probe_out.detach().to("cpu", copy=True))
                x0_latent = guider.model_patcher.model.process_latent_out(
                    captured[-1].detach().to("cpu", copy=True))
                previews.append(_label_index(_decode_preview(vae, x0_latent),
                                             len(endpoints) - 1, seed=seed_i))

            if not endpoints:
                raise ValueError("KSampler Multi-Choice: probe pass captured no candidates")
            preview_sheet = _cat_pad(previews)
            images = [_to_data_url(preview_sheet[i:i + 1]) for i in range(preview_sheet.shape[0])]
            if nid:
                _CACHE[nid] = {"sig": sig, "endpoints": endpoints,
                               "preview_sheet": preview_sheet, "images": images,
                               "base_seed": base_seed, "n_probe": n_probe,
                               "queued_picks": [], "done": set()}

            pick = min(max(0, _await_pick(nid, images, timeout_sec)), len(endpoints) - 1)

        seed_pick = base_seed + pick

        # Continue the picked seed's trajectory from the probe endpoint with the
        # remaining sigmas and zero noise (split-sigma continuation — exact for both
        # EPS and flow/CONST models); with a deterministic sampler the result is
        # identical to a full render of that seed.
        if n_probe < sigmas.shape[-1] - 1:
            print(f"[MultiChoice] continuing seed {seed_pick} from probe step {n_probe} "
                  f"({sigmas.shape[-1] - 1 - n_probe} steps left)")
            cont = latent_image.copy()
            cont["samples"] = endpoints[pick]
            out, out_denoised = _finish_sample(guider, sampler, _ZeroNoise(seed_pick),
                                               cont, sigmas[n_probe:])
        else:
            # The probe already covered the whole schedule — the endpoint is final.
            print(f"[MultiChoice] probe covered the full schedule — seed {seed_pick} is done")
            out = latent_image.copy()
            out.pop("downscale_ratio_spacial", None)
            out.pop("downscale_ratio_temporal", None)
            out["samples"] = endpoints[pick]
            out_denoised = out

        done = [pick]
        if nid and nid in _CACHE:
            _CACHE[nid]["done"].add(pick)
            done = sorted(_CACHE[nid]["done"])
        # The grid stays clickable after the run: clicking another candidate
        # re-queues the prompt and renders it from the cached probe endpoint.
        _send_event("multichoice.finished", {"node_id": nid, "pick": pick, "done": done})

        info = "\n".join([f"picked candidate {pick} -> seed {seed_pick}"] +
                         [f"candidate {i}: seed {base_seed + i}, probed {n_probe} step(s)"
                          for i in range(len(endpoints))])
        return (out, out_denoised, preview_sheet, info)


NODE_CLASS_MAPPINGS = {
    "KSamplerMultiChoice": KSamplerMultiChoice,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "KSamplerMultiChoice": "KSampler (Multi-Choice)",
}
