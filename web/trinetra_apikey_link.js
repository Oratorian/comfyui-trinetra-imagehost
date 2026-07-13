// Trinetra: Upload Image — "Get your API key" helper link above the api_key field.
//
// ComfyUI's Python widgets can't render a clickable hyperlink, so this adds a
// small DOM widget with an <a> that opens the Trinetra app (or whatever base_url
// points at, for self-hosted instances). Keys are created in the app's API tab
// ("Create key"); the app is a SPA with no dedicated key URL, so we link the root.

import { app } from "../../scripts/app.js";

const NODE_NAME = "TrinetraUploadImage";
const DEFAULT_BASE = "https://trinetra.mahesvara.cloud";

function findWidget(node, name) {
    return (node.widgets || []).find((w) => w.name === name);
}

// Derive the app origin from a base_url (strip trailing slashes / /api paths).
function appOrigin(baseUrl) {
    let u = (baseUrl || "").trim() || DEFAULT_BASE;
    try {
        return new URL(u).origin;
    } catch (e) {
        return DEFAULT_BASE;
    }
}

app.registerExtension({
    name: "trinetra.upload.apiKeyLink",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;

            const apiKey = findWidget(this, "api_key");
            const baseUrl = findWidget(this, "base_url");
            if (!apiKey) return r;

            // The info box. Generous padding + vertical margins so it doesn't
            // crowd the api_key field below it.
            const box = document.createElement("div");
            box.style.cssText =
                "box-sizing:border-box;font-size:11px;line-height:1.5;" +
                "padding:9px 11px;margin:8px 4px 10px 4px;" +
                "border:1px solid rgba(120,160,255,0.45);border-radius:7px;" +
                "background:rgba(90,130,255,0.12);color:#cbd6ff;";

            const link = document.createElement("a");
            link.textContent = "🔑 Get your API key here";
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.style.cssText =
                "display:inline-block;color:#8fb4ff;font-weight:600;" +
                "text-decoration:underline;cursor:pointer;";

            const hint = document.createElement("div");
            hint.textContent = "Open the API tab, then “Create key” (starts with tri_).";
            hint.style.cssText = "margin-top:5px;opacity:0.85;";

            box.appendChild(link);
            box.appendChild(hint);

            const syncHref = () => {
                link.href = appOrigin(baseUrl ? baseUrl.value : DEFAULT_BASE);
            };
            syncHref();
            // Keep the link in step if the user edits base_url (self-hosted).
            if (baseUrl) {
                const prev = baseUrl.callback;
                baseUrl.callback = function () {
                    const ret = prev ? prev.apply(this, arguments) : undefined;
                    syncHref();
                    return ret;
                };
            }

            const linkWidget = this.addDOMWidget("apikey_link", "note", box, {
                serialize: false,
            });

            // Reserve enough vertical space so the box isn't clipped or crowded
            // by the api_key field below it. Without an explicit height, a DOM
            // "note" widget collapses to a single row and overlaps its neighbour.
            const BOX_HEIGHT = 62; // px: fits link + hint + padding + margins
            linkWidget.computeSize = () => [0, BOX_HEIGHT];

            // Position the box directly ABOVE the api_key widget.
            try {
                const ws = this.widgets;
                const li = ws.indexOf(linkWidget);
                if (li !== -1) ws.splice(li, 1); // pull from wherever it was appended
                const ki = ws.indexOf(apiKey);
                if (ki !== -1) ws.splice(ki, 0, linkWidget); // insert just before api_key
                else ws.unshift(linkWidget);
            } catch (e) {
                /* leave default (appended) position */
            }

            return r;
        };
    },
});
