// Trinetra: Upload Image — folder picker by name.
//
// Replaces the numeric `folder` field's UX with a combo populated live from the
// account's folders (GET /api/folders, proxied by the /trinetra/folders route).
// The dropdown DISPLAYS folder names but the value sent to the server is the
// folder id. A Refresh button re-fetches using the key currently on the node.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_NAME = "TrinetraUploadImage";
const ROOT_LABEL = "(root — no folder)";

function findWidget(node, name) {
    return (node.widgets || []).find((w) => w.name === name);
}

// name -> id map, kept per-node so the combo label can resolve to an id.
function buildOptions(folders) {
    // Each entry: { label, id }. Root maps to -1 (node treats <0 as "omit").
    const opts = [{ label: ROOT_LABEL, id: -1 }];
    for (const f of folders) {
        const count =
            typeof f.image_count === "number" ? ` · ${f.image_count} img` : "";
        opts.push({ label: `${f.name}  (id ${f.id}${count})`, id: f.id });
    }
    return opts;
}

app.registerExtension({
    name: "trinetra.upload.folderPicker",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            const node = this;

            const folderInt = findWidget(node, "folder"); // the real INT value
            if (!folderInt) return r;

            // State: current option list.
            let options = buildOptions([]);
            const labelForId = (id) => {
                const o = options.find((o) => o.id === id);
                return o ? o.label : `id ${id}`;
            };

            // A combo widget that shows names; writing it updates folderInt.
            const combo = node.addWidget(
                "combo",
                "folder_name",
                labelForId(folderInt.value ?? -1),
                (label) => {
                    const chosen = options.find((o) => o.label === label);
                    if (chosen) folderInt.value = chosen.id;
                },
                { values: () => options.map((o) => o.label), serialize: false }
            );

            // Status line (errors / "Loaded N folders").
            const status = document.createElement("div");
            status.style.cssText =
                "font-size:10px;color:#8a8;padding:2px 0 0 2px;min-height:12px;";
            const statusWidget = node.addDOMWidget("folder_status", "note", status, {
                serialize: false,
            });

            const refresh = async () => {
                const apiKey = findWidget(node, "api_key")?.value || "";
                const baseUrl = findWidget(node, "base_url")?.value || "";
                const authScheme = findWidget(node, "auth_scheme")?.value || "bearer";
                if (!apiKey.trim()) {
                    status.style.color = "#e0a030";
                    status.textContent = "Set api_key first, then Refresh folders.";
                    return;
                }
                status.style.color = "#8a8";
                status.textContent = "Loading folders…";
                try {
                    const resp = await api.fetchApi("/trinetra/folders", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            api_key: apiKey,
                            base_url: baseUrl,
                            auth_scheme: authScheme,
                        }),
                    });
                    const data = await resp.json();
                    if (!data.ok) {
                        status.style.color = "#e06060";
                        status.textContent = "Error: " + (data.error || "unknown");
                        return;
                    }
                    options = buildOptions(data.folders || []);
                    // Keep the current id selected if it still exists.
                    const keepId = folderInt.value ?? -1;
                    combo.value = labelForId(keepId);
                    status.style.color = "#8a8";
                    status.textContent = `Loaded ${data.folders.length} folder(s).`;
                    node.setDirtyCanvas(true, true);
                } catch (e) {
                    status.style.color = "#e06060";
                    status.textContent = "Request failed: " + e;
                }
            };

            node.addWidget("button", "🔄 Refresh folders", null, refresh);

            // Hide the raw INT widget to avoid two controls for one value; its
            // value is still what the node serializes and sends.
            if (folderInt.type !== "hidden") {
                folderInt.origType = folderInt.type;
                folderInt.type = "hidden";
                folderInt.computeSize = () => [0, -4]; // collapse its row
            }

            return r;
        };
    },
});
