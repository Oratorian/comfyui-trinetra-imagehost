"""ComfyUI custom node package: Trinetra image host uploader.

Registers the upload node and exposes a web directory for the front-end
date/time picker used by the "On date..." expiry option.
"""

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

# Front-end assets (JS date-picker + folder dropdown). Relative to this package.
WEB_DIRECTORY = "./web"

# Register the /trinetra/folders proxy route (best-effort; no-op outside ComfyUI).
try:
    from .server_routes import register_routes

    register_routes()
except Exception:
    pass

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
