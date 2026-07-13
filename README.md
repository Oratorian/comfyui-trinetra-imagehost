# ComfyUI Trinetra Image Host Uploader

A ComfyUI custom node that uploads images to a [Trinetra](https://trinetra.mahesvara.cloud)
image host using your API key, and **respects rate limits** by backing off on
`429` (honoring `Retry-After` when present) and retrying transient server errors.

## Node

**Trinetra: Upload Image** (`image/Trinetra`)

Takes an `IMAGE` (single or batch) and uploads each frame to
`POST /api/images`. Outputs:

| Output        | Type   | Description                                   |
|---------------|--------|-----------------------------------------------|
| `url`         | STRING | URL of the first uploaded image               |
| `all_urls`    | STRING | Newline-joined URLs for the whole batch       |
| `json_report` | STRING | Per-image report (url + attempt count)        |

### Inputs

**Required**
- `images` - the image(s) to upload.
- `api_key` - your Trinetra key (`tri_...`).

**Optional**
- `base_url` - defaults to `https://trinetra.mahesvara.cloud`.
- `auth_scheme` - `bearer` (`Authorization: Bearer`) or `x-api-key` (`X-API-Key`).
- `format` - `PNG` (lossless, keeps alpha), `JPEG`, or `WEBP`.
- `quality` - 1-100, used for JPEG/WEBP.
- `folder` - Trinetra folder id to upload into. `-1` = omit (root).
- `ttl` - auto-delete rule:
  - empty or `never` = permanent
  - relative seconds: `3600` (1h), `86400` (1d), `604800` (7d), `2592000` (30d)
  - absolute: `at:<epoch-ms>` (must be in the future)
- `max_attempts` - total tries per image before failing (default 6).
- `base_delay_seconds` / `max_delay_seconds` - exponential backoff bounds.
- `timeout_seconds` - per-request network timeout.

## Rate limiting behavior

The upload endpoint has a dedicated lane (default 300/min per user). This node:

1. **Honors `Retry-After`** exactly when the server sends it on a `429`
   (capped at `max_retry_after`, default 300s, to avoid pathological waits).
2. Otherwise uses **exponential backoff with full jitter**, bounded by
   `max_delay_seconds`.
3. Retries transient **`5xx`** and network errors the same way.
4. **Proactively throttles**: if the server exposes `X-RateLimit-Remaining` /
   `X-RateLimit-Reset` and your budget hits `0`, it sleeps until the window
   resets before the next upload, so a batch does not slam into a wall of 429s.
5. Fails fast on `400` / `401` / `413` with the server's own error message
   (retrying those never helps).

Backoff decisions are printed to the ComfyUI console (`[Trinetra] ...`).

## Installation

Clone into your `ComfyUI/custom_nodes/` directory:

```
cd ComfyUI/custom_nodes
git clone <this-repo> comfyui-trinetra-imagehost
```

Dependencies (`Pillow`, `numpy`) ship with ComfyUI. Restart ComfyUI and the
node appears under **image/Trinetra**.

## Testing

The rate-limit / retry logic, image encoding, and expiry validation are covered
by offline tests (no network) under `tests/`:

```
python tests/test_trinetra_client.py   # backoff / retry / error handling
python tests/test_review_fixes.py       # regression tests for reviewed fixes
python tests/test_node_e2e.py           # end-to-end node (needs Pillow + numpy)
```

Tests live in the repo for contributors and CI, but are excluded from any built
package (see `pyproject.toml`).

## Security note

Your API key is entered on the node as plain text and is saved in the
workflow JSON. Do not share exported workflows that contain a real key.
