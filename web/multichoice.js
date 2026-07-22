import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// KSampler Multi-Choice: while the Python node blocks awaiting a pick, it pushes the
// candidate previews here; we draw them as a clickable grid on the node and POST
// the clicked index back, which unblocks sampling.

const MARGIN = 8;
const GAP = 4;

function gridTop(node) {
    let top = 120;
    const ws = node.widgets;
    if (ws?.length) {
        const ly = ws[ws.length - 1].last_y;
        if (typeof ly === "number") top = ly + LiteGraph.NODE_WIDGET_HEIGHT + 10;
    }
    return top;
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

app.registerExtension({
    name: "KSamplerMultiChoice",

    setup() {
        api.addEventListener("multichoice.candidates", (e) => {
            const { node_id, images, waiting } = e.detail;
            const node = app.graph.getNodeById(Number(node_id));
            if (!node) return;
            node.mcImages = images.map((src) => {
                const img = new Image();
                img.onload = () => node.setDirtyCanvas(true, true);
                img.src = src;
                return img;
            });
            node.mcPicked = -1;
            node.mcWaiting = waiting !== false;
            if (node.mcWaiting) node.mcDone = [];
            node.mcFinished = false;
            // Make room for the grid if the node is small.
            const top = gridTop(node);
            const rows = Math.ceil(node.mcImages.length / Math.ceil(Math.sqrt(node.mcImages.length)));
            node.setSize([Math.max(node.size[0], 380), Math.max(node.size[1], top + rows * 130 + MARGIN)]);
            node.setDirtyCanvas(true, true);
        });

        api.addEventListener("multichoice.done", (e) => {
            const node = app.graph.getNodeById(Number(e.detail.node_id));
            if (!node) return;
            node.mcWaiting = false;
            node.setDirtyCanvas(true, true);
        });

        // Run finished: the candidates stay clickable — picking another one
        // re-queues the prompt and it renders from the cached probe endpoint.
        api.addEventListener("multichoice.finished", (e) => {
            const { node_id, pick, done } = e.detail;
            const node = app.graph.getNodeById(Number(node_id));
            if (!node) return;
            node.mcWaiting = false;
            node.mcFinished = true;
            node.mcPicked = pick;
            node.mcDone = done || [];
            node.setDirtyCanvas(true, true);
        });
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
            if (this.mcWaiting || this.mcFinished) {
                ctx.fillStyle = "#4f4";
                ctx.font = "12px Arial";
                ctx.textAlign = "center";
                ctx.fillText(this.mcWaiting ? "click a candidate to continue sampling"
                                            : "click another candidate to render it too",
                             this.size[0] / 2, g.top - 2);
            }
            ctx.restore();
        };

        const origMouse = node.onMouseDown;
        node.onMouseDown = function (e, pos) {
            if ((this.mcWaiting || this.mcFinished) && this.mcImages?.length) {
                const g = gridLayout(this);
                if (g && pos[1] >= g.top) {
                    const col = Math.floor((pos[0] - MARGIN) / g.cw);
                    const row = Math.floor((pos[1] - g.top) / g.ch);
                    const i = row * g.cols + col;
                    if (col >= 0 && col < g.cols && i >= 0 && i < this.mcImages.length) {
                        this.mcPicked = i;
                        this.mcWaiting = false;
                        this.setDirtyCanvas(true, true);
                        api.fetchApi("/multichoice/pick", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ node_id: this.id, pick: i }),
                        }).then(async (r) => {
                            const j = await r.json();
                            // Pick landed after the run finished — re-queue; the node
                            // renders it from the cached probe endpoint (no re-probe).
                            if (j?.mode === "queued") app.queuePrompt(0);
                        }).catch(() => {});
                        return true;
                    }
                }
            }
            return origMouse?.apply(this, arguments);
        };
    },
});
