// Trinetra: Upload Image — date/time picker for the "On date..." expiry option.
//
// When the `expiry` dropdown is set to "On date...", this adds a native
// <input type="datetime-local"> DOM widget to the node. Picking a local date/time
// converts it to the API's absolute form `at:<epoch-ms>` and stores it in the
// hidden `ttl_at` string widget, so it serializes with the workflow and the
// Python side needs no extra input. For any other expiry value the picker hides.
// There is deliberately no free-text expiry entry — a bogus ttl would only fail
// after the upload was spent.

import { app } from "../../scripts/app.js";

const NODE_NAME = "TrinetraUploadImage";
const ON_DATE = "On date...";

function findWidget(node, name) {
    return (node.widgets || []).find((w) => w.name === name);
}

// Convert a datetime-local string (local wall-clock, no tz) to `at:<epoch-ms>`.
function toAtEpochMs(localValue) {
    if (!localValue) return "";
    const ms = new Date(localValue).getTime(); // interpreted as local time
    if (Number.isNaN(ms)) return "";
    return "at:" + ms;
}

// Pre-fill the picker from an existing `at:<epoch-ms>` value, if present.
function fromTtlCustom(value) {
    if (typeof value !== "string" || !value.startsWith("at:")) return "";
    const ms = Number(value.slice(3));
    if (!Number.isFinite(ms)) return "";
    const d = new Date(ms);
    // Format back to a datetime-local string in local time: YYYY-MM-DDTHH:MM
    const pad = (n) => String(n).padStart(2, "0");
    return (
        d.getFullYear() +
        "-" + pad(d.getMonth() + 1) +
        "-" + pad(d.getDate()) +
        "T" + pad(d.getHours()) +
        ":" + pad(d.getMinutes())
    );
}

app.registerExtension({
    name: "trinetra.upload.expiryPicker",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;

            const expiry = findWidget(this, "expiry");
            const ttlAt = findWidget(this, "ttl_at"); // hidden; JS-only storage
            if (!expiry || !ttlAt) return r;

            // Keep the hidden storage field permanently collapsed — the user
            // never sees or types into it; the picker is the only writer.
            ttlAt.type = "hidden";
            ttlAt.computeSize = () => [0, -4];

            // Build the datetime-local input.
            const input = document.createElement("input");
            input.type = "datetime-local";
            input.style.width = "100%";
            input.style.boxSizing = "border-box";
            // Seed from any existing at:<ms> value saved in the workflow.
            input.value = fromTtlCustom(ttlAt.value);

            const dateWidget = this.addDOMWidget(
                "expiry_datetime",
                "datetime",
                input,
                {
                    // We serialize via ttl_at, so don't double-store this.
                    serialize: false,
                    getValue: () => input.value,
                    setValue: (v) => { input.value = v || ""; },
                }
            );

            const warn = document.createElement("div");
            warn.style.cssText =
                "font-size:10px;color:#e0a030;padding:2px 0 0 2px;min-height:12px;";
            const warnWidget = this.addDOMWidget("expiry_warn", "note", warn, {
                serialize: false,
            });

            // Reorder: move the picker + warning to sit right after `expiry`
            // instead of at the bottom of the node. Best-effort; if the widget
            // layout differs in this ComfyUI version, we just leave them appended.
            try {
                const ws = this.widgets;
                const pull = (w) => {
                    const i = ws.indexOf(w);
                    if (i !== -1) ws.splice(i, 1);
                };
                pull(dateWidget);
                pull(warnWidget);
                const expiryIdx = ws.indexOf(expiry);
                if (expiryIdx !== -1) {
                    ws.splice(expiryIdx + 1, 0, dateWidget, warnWidget);
                } else {
                    ws.push(dateWidget, warnWidget);
                }
            } catch (e) {
                /* leave default order */
            }

            const applyDate = () => {
                const raw = toAtEpochMs(input.value);
                if (!raw) {
                    warn.textContent = "";
                    ttlAt.value = ""; // no valid date -> clear storage
                    return;
                }
                const ms = Number(raw.slice(3));
                warn.textContent = ms <= Date.now()
                    ? "⚠ time is in the past — server will reject it"
                    : "";
                ttlAt.value = raw; // validated 'at:<ms>'; this is what serializes
                this.setDirtyCanvas(true, true);
            };
            input.addEventListener("change", applyDate);
            input.addEventListener("input", applyDate);

            // DOM widgets (dateWidget/warnWidget) hide via their .element.
            const setDomHidden = (w, hidden) => {
                if (w && w.element) w.element.style.display = hidden ? "none" : "";
            };

            // Show the calendar only for "On date..."; every preset hides it.
            const syncVisibility = () => {
                const onDate = expiry.value === ON_DATE;
                setDomHidden(dateWidget, !onDate);
                setDomHidden(warnWidget, !onDate);
                if (onDate) applyDate();
                // Nudge the node to recompute its height for the shown/hidden rows.
                this.setDirtyCanvas(true, true);
                requestAnimationFrame(() => {
                    const sz = this.computeSize();
                    this.setSize([Math.max(this.size[0], sz[0]), sz[1]]);
                });
            };

            // Wrap the dropdown's callback to react to selection changes.
            const prevCallback = expiry.callback;
            expiry.callback = function () {
                const ret = prevCallback ? prevCallback.apply(this, arguments) : undefined;
                syncVisibility();
                return ret;
            };

            // Initial state (also handles a reloaded workflow that saved "On date...").
            requestAnimationFrame(syncVisibility);

            return r;
        };
    },
});
