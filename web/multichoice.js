import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// KSampler Multi-Choice: while the Python node blocks awaiting a pick, it pushes the
// candidate previews here; we draw them as a clickable grid on the node and POST
// the clicked index back, which unblocks sampling.
//
// Controls:  click a thumbnail (or press its 0-9 number) to finish that
// candidate;  shift-hover enlarges a thumbnail.  After the run, clicks and
// number keys keep working — each one queues that candidate for an immediate
// re-render from its cached probe endpoint.

const MARGIN = 8;
const GAP = 4;
const TEXT_BAND = 16; // reserved strip between the widgets and the grid for the status line

// node ids currently blocked waiting for a pick — targets of the 0-9 key shortcut
const WAITING = new Set();
// the multichoice node that most recently showed candidates — post-run key target
let lastActiveId = null;

function gridTop(node) {
    let top = 120;
    const ws = node.widgets;
    if (ws?.length) {
        const ly = ws[ws.length - 1].last_y;
        if (typeof ly === "number") top = ly + LiteGraph.NODE_WIDGET_HEIGHT + 10;
    }
    return top + TEXT_BAND;
}

function gridLayout(node) {
    const n = node.mcImages?.length || 0;
    if (!n) return null;
    const top = gridTop(node);
    const w = node.size[0] - MARGIN * 2;
    const h = node.size[1] - top - MARGIN;
    if (w <= 0 || h <= 0) return null;
    const cols = Math.ceil(Math.sqrt(n));
    const rows = Math.ceil(n / cols);
    return { top, cols, rows, cw: w / cols, ch: h / rows };
}

function hitIndex(node, pos) {
    const g = gridLayout(node);
    if (!g || pos[1] < g.top) return -1;
    const col = Math.floor((pos[0] - MARGIN) / g.cw);
    const row = Math.floor((pos[1] - g.top) / g.ch);
    const i = row * g.cols + col;
    return col >= 0 && col < g.cols && i >= 0 && i < (node.mcImages?.length || 0) ? i : -1;
}

function postPick(node, i) {
    node.mcPicked = i;
    node.mcWaiting = false;
    node.setDirtyCanvas(true, true);
    api.fetchApi("/multichoice/pick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node_id: node.id, pick: i }),
    }).then(async (r) => {
        const j = await r.json();
        // Pick landed after the run finished — re-queue; the node renders it
        // from the cached probe endpoint (no re-probe).
        if (j?.mode === "queued") app.queuePrompt(0);
    }).catch(() => {});
}

// The node the 0-9 keys should act on: a node that is actually waiting if there
// is one (stale WAITING entries are pruned so a missed event can't jam this),
// otherwise the most recent finished grid (keys then queue re-renders).
function keyTarget() {
    for (const id of [...WAITING]) {
        const n = app.graph.getNodeById(Number(id));
        if (n?.mcWaiting && n.mcImages?.length) return n;
        WAITING.delete(id);
    }
    if (lastActiveId != null) {
        const n = app.graph.getNodeById(Number(lastActiveId));
        if (n?.mcFinished && n.mcImages?.length) return n;
    }
    return null;
}

app.registerExtension({
    name: "KSamplerMultiChoice",

    setup() {
        api.addEventListener("multichoice.candidates", (e) => {
            const { node_id, images, waiting } = e.detail;
            const node = app.graph.getNodeById(Number(node_id));
            if (!node) return;
            lastActiveId = String(node_id);
            node.mcImages = images.map((src) => {
                const img = new Image();
                img.onload = () => node.setDirtyCanvas(true, true);
                img.src = src;
                return img;
            });
            node.mcPicked = -1;
            node.mcZoom = -1;
            node.mcWaiting = waiting !== false;
            if (node.mcWaiting) {
                node.mcDone = [];
                WAITING.add(String(node_id));
            }
            node.mcFinished = false;
            // Make room for the grid if the node is small.
            const top = gridTop(node);
            const rows = Math.ceil(node.mcImages.length / Math.ceil(Math.sqrt(node.mcImages.length)));
            node.setSize([Math.max(node.size[0], 380), Math.max(node.size[1], top + rows * 130 + MARGIN)]);
            node.setDirtyCanvas(true, true);
        });

        api.addEventListener("multichoice.done", (e) => {
            WAITING.delete(String(e.detail.node_id));
            const node = app.graph.getNodeById(Number(e.detail.node_id));
            if (!node) return;
            node.mcWaiting = false;
            node.setDirtyCanvas(true, true);
        });

        // Run finished: the candidates stay clickable — picking another one
        // re-queues the prompt and it renders from the cached probe endpoint.
        api.addEventListener("multichoice.finished", (e) => {
            const { node_id, pick, done } = e.detail;
            WAITING.delete(String(node_id));
            const node = app.graph.getNodeById(Number(node_id));
            if (!node) return;
            lastActiveId = String(node_id);
            node.mcWaiting = false;
            node.mcFinished = true;
            node.mcPicked = pick;
            node.mcDone = done || [];
            node.setDirtyCanvas(true, true);
        });

        // Number keys pick by the index stamped on each thumbnail (0-9). While
        // waiting they finish that candidate; after the run they queue it for a
        // re-render. Capture phase on window so the canvas key handling can't
        // swallow the event first.
        window.addEventListener("keydown", (ev) => {
            if (!/^[0-9]$/.test(ev.key) || ev.ctrlKey || ev.altKey || ev.metaKey) return;
            const t = ev.target;
            if (t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t?.isContentEditable) return;
            const node = keyTarget();
            const i = parseInt(ev.key, 10);
            if (!node || i >= (node.mcImages?.length || 0)) return;
            ev.preventDefault();
            ev.stopImmediatePropagation();
            postPick(node, i);
        }, true);
    },

    async nodeCreated(node) {
        if (node.comfyClass !== "KSamplerMultiChoice") return;

        const origDraw = node.onDrawBackground;
        node.onDrawBackground = function (ctx) {
            origDraw?.apply(this, arguments);
            if (this.flags?.collapsed) return;
            const g = gridLayout(this);
            if (!g) return;
            const imgs = this.mcImages;
            ctx.save();
            for (let i = 0; i < imgs.length; i++) {
                const col = i % g.cols, row = Math.floor(i / g.cols);
                const x = MARGIN + col * g.cw + GAP / 2;
                const y = g.top + row * g.ch + GAP / 2;
                const w = g.cw - GAP, h = g.ch - GAP;
                const img = imgs[i];
                if (img.complete && img.naturalWidth) {
                    const s = Math.min(w / img.naturalWidth, h / img.naturalHeight);
                    const dw = img.naturalWidth * s, dh = img.naturalHeight * s;
                    ctx.drawImage(img, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh);
                } else {
                    ctx.fillStyle = "#333";
                    ctx.fillRect(x, y, w, h);
                }
                if (this.mcDone?.includes(i)) {
                    // Already rendered — tick in the top-right corner.
                    ctx.fillStyle = "rgba(0,0,0,0.6)";
                    ctx.fillRect(x + w - 22, y + 2, 20, 16);
                    ctx.fillStyle = "#4f4";
                    ctx.font = "bold 12px Arial";
                    ctx.textAlign = "center";
                    ctx.fillText("✓", x + w - 12, y + 14);
                }
                if (i === this.mcPicked) {
                    ctx.strokeStyle = "#4f4";
                    ctx.lineWidth = 3;
                    ctx.strokeRect(x, y, w, h);
                }
            }
            // Shift-hover: enlarge the hovered thumbnail over the whole grid area.
            const zi = this.mcZoom;
            if (zi >= 0 && zi < imgs.length && imgs[zi].complete && imgs[zi].naturalWidth) {
                const gx = MARGIN, gy = g.top;
                const gw = this.size[0] - MARGIN * 2, gh = this.size[1] - g.top - MARGIN;
                ctx.fillStyle = "rgba(0,0,0,0.85)";
                ctx.fillRect(gx, gy, gw, gh);
                const img = imgs[zi];
                const s = Math.min(gw / img.naturalWidth, gh / img.naturalHeight);
                const dw = img.naturalWidth * s, dh = img.naturalHeight * s;
                ctx.drawImage(img, gx + (gw - dw) / 2, gy + (gh - dh) / 2, dw, dh);
            }
            if (this.mcWaiting || this.mcFinished) {
                ctx.fillStyle = "#4f4";
                ctx.font = "12px Arial";
                ctx.textAlign = "center";
                ctx.fillText(this.mcWaiting
                    ? "click or keys 0-9 to finish a candidate · shift-hover zooms"
                    : "click or keys 0-9 to render another candidate",
                    this.size[0] / 2, g.top - 5);
            }
            ctx.restore();
        };

        const origMouse = node.onMouseDown;
        node.onMouseDown = function (e, pos) {
            if ((this.mcWaiting || this.mcFinished) && this.mcImages?.length) {
                const i = hitIndex(this, pos);
                if (i >= 0) {
                    postPick(this, i);
                    return true;
                }
            }
            return origMouse?.apply(this, arguments);
        };

        const origMove = node.onMouseMove;
        node.onMouseMove = function (e, pos) {
            if (this.mcImages?.length) {
                const zi = e.shiftKey ? hitIndex(this, pos) : -1;
                if (zi !== this.mcZoom) {
                    this.mcZoom = zi;
                    this.setDirtyCanvas(true, true);
                }
            }
            return origMove?.apply(this, arguments);
        };

        const origLeave = node.onMouseLeave;
        node.onMouseLeave = function () {
            if (this.mcZoom !== -1) {
                this.mcZoom = -1;
                this.setDirtyCanvas(true, true);
            }
            return origLeave?.apply(this, arguments);
        };
    },
});
