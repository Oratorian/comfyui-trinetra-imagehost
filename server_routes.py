"""ComfyUI server route: proxy the Trinetra folder list to the front-end.

The folder dropdown must be populated with the user's own folders, which requires
their API key, which is only known at node-edit time in the browser. So the JS
widget calls this route (POST /trinetra/folders) with the key/base_url/auth from
the node, and we proxy to Trinetra's GET /api/folders server-side (avoiding CORS
and keeping the request identical to the upload path).

Registration is best-effort: if PromptServer/aiohttp are unavailable (e.g. running
the node's tests outside ComfyUI), we simply skip it.
"""

from __future__ import annotations

from .trinetra_client import DEFAULT_BASE_URL, TrinetraError, list_folders


def register_routes() -> bool:
    """Register the /trinetra/folders route on ComfyUI's server. Returns success."""
    try:
        from server import PromptServer  # provided by ComfyUI at runtime
        from aiohttp import web
    except Exception:
        # Not running inside ComfyUI (or an incompatible version): skip silently.
        return False

    routes = PromptServer.instance.routes

    @routes.post("/trinetra/folders")
    async def _trinetra_folders(request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        api_key = (body.get("api_key") or "").strip()
        base_url = (body.get("base_url") or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        auth_scheme = body.get("auth_scheme") or "bearer"

        if not api_key:
            return web.json_response(
                {"ok": False, "error": "No API key set on the node."}, status=200
            )

        try:
            # Blocking urllib call; folder lists are tiny so this is fine.
            folders = list_folders(
                api_key, base_url=base_url, auth_scheme=auth_scheme, timeout=20.0
            )
        except TrinetraError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=200)
        except Exception as exc:  # never 500 the ComfyUI server over this
            return web.json_response(
                {"ok": False, "error": f"Unexpected error: {exc}"}, status=200
            )

        # Return a compact, name-sorted list for the dropdown.
        slim = [
            {
                "id": f.get("id"),
                "name": f.get("name") or f"folder {f.get('id')}",
                "parent_id": f.get("parent_id"),
                "image_count": f.get("image_count"),
            }
            for f in folders
        ]
        slim.sort(key=lambda f: (str(f["name"]).lower(), f["id"]))
        return web.json_response({"ok": True, "folders": slim}, status=200)

    return True
