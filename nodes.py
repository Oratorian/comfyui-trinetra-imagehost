"""ComfyUI node definitions for uploading images to the Trinetra host."""

from __future__ import annotations

import io
from typing import Any, List

import numpy as np
from PIL import Image

from .trinetra_client import (
    DEFAULT_BASE_URL,
    RetryConfig,
    TrinetraError,
    upload_image,
)

# Maps the friendly dropdown label -> the raw `ttl` value the API expects.
# None means "omit ttl" (permanent). "On date..." defers to the JS date picker,
# which fills the hidden `ttl_at` widget with a validated 'at:<epoch-ms>' value.
# There is deliberately NO free-text option: a bogus ttl would be sent to the
# server (or reject the upload) only AFTER the image bytes were spent.
# Insertion order here is the order shown in the ComfyUI dropdown.
TTL_PRESETS = {
    "Never (permanent)": None,
    "1 hour": "3600",
    "6 hours": "21600",
    "1 day": "86400",
    "7 days": "604800",
    "30 days": "2592000",
    "On date...": "__at__",
}


def _resolve_ttl(expiry: str, ttl_at: str):
    """Resolve the dropdown selection into a raw ttl string or None.

    For "On date...", ``ttl_at`` must be exactly ``at:<positive-integer-ms>``
    (produced by the validated date picker). Anything malformed raises so the
    upload never proceeds with a garbage expiry.
    """
    raw = TTL_PRESETS.get(expiry, None)
    if raw != "__at__":
        return raw
    value = (ttl_at or "").strip()
    if not value:
        raise ValueError(
            "Expiry is 'On date...' but no date was picked. "
            "Choose a date/time or switch expiry to a preset."
        )
    if not value.startswith("at:") or not value[3:].isdigit() or int(value[3:]) <= 0:
        raise ValueError(
            f"Invalid absolute expiry {value!r}; expected 'at:<epoch-ms>'. "
            "Re-pick the date in the calendar."
        )
    return value


def _coerce_float(value, default: float, lo: float, hi: float) -> float:
    """Best-effort float in [lo, hi]; empty/invalid -> default, then clamped.

    ComfyUI can hand a widget through as an empty string (e.g. a stale workflow
    saved under a different input layout). A bare float() would crash, so we
    fall back to the default and clamp into the widget's declared range instead.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = default
    return max(lo, min(hi, f))


def _coerce_int(value, default: int, lo: int, hi: int) -> int:
    """Best-effort int in [lo, hi]; empty/invalid -> default, then clamped."""
    try:
        i = int(float(value))  # float() first so "6.0"/"6" both work
    except (TypeError, ValueError):
        i = default
    return max(lo, min(hi, i))


def _tensor_to_pil_list(image: Any) -> List[Image.Image]:
    """Convert a ComfyUI IMAGE batch tensor into a list of PIL images.

    ComfyUI IMAGE is a torch tensor shaped [B, H, W, C] with float values in
    [0, 1]. We convert without importing torch directly (numpy handles it).
    """
    arr = image.cpu().numpy() if hasattr(image, "cpu") else np.asarray(image)
    if arr.ndim == 3:
        arr = arr[None, ...]  # promote single image to a batch of one

    out: List[Image.Image] = []
    for frame in arr:
        # Sanitize non-finite values: NaN/inf survive np.clip and cast to
        # garbage uint8, so map them to defined pixels before scaling.
        frame = np.nan_to_num(frame, nan=0.0, posinf=1.0, neginf=0.0)
        clipped = np.clip(frame * 255.0, 0, 255).astype(np.uint8)
        channels = clipped.shape[-1] if clipped.ndim == 3 else 1
        if channels == 4:
            out.append(Image.fromarray(np.ascontiguousarray(clipped), "RGBA"))
        elif channels == 3:
            out.append(Image.fromarray(np.ascontiguousarray(clipped[..., :3]), "RGB"))
        elif channels == 1:
            plane = clipped[..., 0] if clipped.ndim == 3 else clipped
            out.append(Image.fromarray(plane, "L").convert("RGB"))
        elif channels == 2:
            # 2-channel (luminance + alpha): treat channel 0 as gray, 1 as alpha.
            la = np.ascontiguousarray(clipped[..., :2])
            out.append(Image.fromarray(la, "LA").convert("RGBA"))
        else:
            raise ValueError(
                f"Unsupported IMAGE channel count: {channels} "
                f"(expected 1, 2, 3, or 4)"
            )
    return out


def _encode(pil_img: Image.Image, fmt: str, quality: int) -> tuple[bytes, str, str]:
    """Encode a PIL image to bytes. Returns (bytes, mime, extension)."""
    fmt = fmt.upper()
    buf = io.BytesIO()
    if fmt == "JPEG":
        # JPEG has no alpha. Composite over white instead of dropping alpha
        # (a bare convert('RGB') would expose whatever RGB hid under alpha=0).
        rgb = _flatten_alpha(pil_img)
        rgb.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), "image/jpeg", "jpg"
    if fmt == "WEBP":
        # quality=100 opts into true lossless; otherwise lossy at the given q.
        if quality >= 100:
            pil_img.save(buf, format="WEBP", lossless=True)
        else:
            pil_img.save(buf, format="WEBP", quality=quality)
        return buf.getvalue(), "image/webp", "webp"
    # default PNG (lossless, keeps alpha)
    pil_img.save(buf, format="PNG", compress_level=6)
    return buf.getvalue(), "image/png", "png"


def _flatten_alpha(pil_img: Image.Image, background=(255, 255, 255)) -> Image.Image:
    """Return an RGB image, compositing any alpha over a solid background."""
    if pil_img.mode in ("RGBA", "LA") or (
        pil_img.mode == "P" and "transparency" in pil_img.info
    ):
        rgba = pil_img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, background)
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return pil_img.convert("RGB")


class TrinetraUploadImage:
    """Upload one or more images to the Trinetra image host.

    Returns the URL(s) of the uploaded image(s). Handles 429 rate limiting by
    backing off (honoring Retry-After when the server sends it) and retries
    transient server errors, so batch uploads stay within the host's limits.
    """

    CATEGORY = "image/Trinetra"
    FUNCTION = "upload"
    # url (first image), all_urls (newline-joined), json_report
    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("url", "all_urls", "json_report")
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "api_key": ("STRING", {"default": "", "multiline": False}),
            },
            "optional": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "auth_scheme": (["bearer", "x-api-key"], {"default": "bearer"}),
                "format": (["PNG", "JPEG", "WEBP"], {"default": "PNG"}),
                "quality": ("INT", {"default": 92, "min": 1, "max": 100}),
                # -1 means "omit folder"; API expects a folder id otherwise.
                "folder": ("INT", {"default": -1, "min": -1, "max": 2**31 - 1}),
                # Friendly preset dropdown. No free-text option by design:
                # a malformed ttl would only fail after the upload is spent.
                "expiry": (
                    list(TTL_PRESETS.keys()),
                    {
                        "default": "Never (permanent)",
                        "tooltip": "When the uploaded image auto-deletes. "
                        "Pick 'On date...' to choose an exact date/time.",
                    },
                ),
                # Hidden field written ONLY by the JS date picker (validated
                # 'at:<epoch-ms>'). Never typed by the user.
                "ttl_at": (
                    "STRING",
                    {"default": "", "multiline": False},
                ),
                "max_attempts": ("INT", {"default": 6, "min": 1, "max": 15}),
                "base_delay_seconds": (
                    "FLOAT",
                    {"default": 1.0, "min": 0.1, "max": 30.0, "step": 0.1},
                ),
                "max_delay_seconds": (
                    "FLOAT",
                    {"default": 60.0, "min": 1.0, "max": 600.0, "step": 1.0},
                ),
                "timeout_seconds": (
                    "FLOAT",
                    {"default": 60.0, "min": 5.0, "max": 600.0, "step": 5.0},
                ),
            },
        }

    def upload(
        self,
        images,
        api_key,
        base_url=DEFAULT_BASE_URL,
        auth_scheme="bearer",
        format="PNG",
        quality=92,
        folder=-1,
        expiry="Never (permanent)",
        ttl_at="",
        max_attempts=6,
        base_delay_seconds=1.0,
        max_delay_seconds=60.0,
        timeout_seconds=60.0,
    ):
        # Coerce the tuning knobs defensively. A workflow saved under an older
        # input layout can deliver these as empty strings or out-of-range values;
        # fall back to the widget defaults and clamp rather than crash. Ranges
        # mirror the INPUT_TYPES declarations above.
        retry = RetryConfig(
            max_attempts=_coerce_int(max_attempts, 6, 1, 15),
            base_delay=_coerce_float(base_delay_seconds, 1.0, 0.1, 30.0),
            max_delay=_coerce_float(max_delay_seconds, 60.0, 1.0, 600.0),
        )
        timeout_seconds = _coerce_float(timeout_seconds, 60.0, 5.0, 600.0)
        quality = _coerce_int(quality, 92, 1, 100)
        folder = _coerce_int(folder, -1, -1, 2**31 - 1)
        try:
            ttl = _resolve_ttl(expiry, ttl_at)
        except ValueError as exc:
            raise RuntimeError(f"Trinetra: {exc}") from exc

        pil_images = _tensor_to_pil_list(images)
        if not pil_images:
            # An empty IMAGE batch is almost always an upstream bug; failing
            # loudly beats silently returning empty URLs downstream.
            raise RuntimeError("Trinetra: no images to upload (empty IMAGE batch).")

        urls: List[str] = []
        reports: List[str] = []

        for idx, pil in enumerate(pil_images):
            data, mime, ext = _encode(pil, format, quality)  # already coerced int
            filename = f"comfyui_{idx:03d}.{ext}"
            try:
                result = upload_image(
                    file_bytes=data,
                    api_key=api_key,
                    base_url=base_url or DEFAULT_BASE_URL,
                    filename=filename,
                    mime=mime,
                    folder=(None if folder < 0 else folder),  # already coerced int
                    ttl=ttl,  # already resolved: raw string or None
                    auth_scheme=auth_scheme,
                    timeout=timeout_seconds,  # already coerced float
                    retry=retry,
                    log=print,  # surfaces backoff messages in the ComfyUI console
                )
            except TrinetraError as exc:
                # Preserve any URLs already uploaded so the user can see what
                # succeeded and avoid re-uploading duplicates on retry.
                done = "; ".join(urls) if urls else "none"
                raise RuntimeError(
                    f"Trinetra upload failed for image {idx}: {exc} "
                    f"(already uploaded {len(urls)} image(s): {done})"
                ) from exc

            urls.append(result.url)
            reports.append(
                f"[{idx}] {result.url}"
                + (f"  (attempts={result.attempts})" if result.attempts > 1 else "")
            )
            print(f"[Trinetra] uploaded image {idx} -> {result.url} in {result.attempts} attempt(s)")

        all_urls = "\n".join(urls)
        report = "\n".join(reports)
        first = urls[0] if urls else ""

        # ui block gives inline text feedback in the ComfyUI node.
        return {
            "ui": {"text": [report]},
            "result": (first, all_urls, report),
        }


NODE_CLASS_MAPPINGS = {
    "TrinetraUploadImage": TrinetraUploadImage,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "TrinetraUploadImage": "Trinetra: Upload Image",
}
