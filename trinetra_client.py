"""HTTP client for the Trinetra image host, with rate-limit aware retries.

The single public entry point is :func:`upload_image`. It uploads one image
via ``POST /api/images`` (multipart/form-data) and returns the parsed Image
object as a dict.

Design goals:
- Respect the server. On ``429`` (and transient ``5xx``) it backs off. If the
  server sends a ``Retry-After`` header it obeys it exactly; otherwise it uses
  exponential backoff with full jitter, capped, and bounded by a max attempt
  count.
- Be proactive. If the server exposes ``X-RateLimit-Remaining`` / ``-Reset``
  and we are down to the last token, we sleep until the window resets *before*
  firing the next request rather than eating an inevitable 429.
- Fail loudly on non-retryable errors (400/401/413) with the server's own
  message, so the node surfaces something actionable to the user.

Only the Python standard library is used so this drops into any ComfyUI env
without extra pip installs.
"""

from __future__ import annotations

import io
import json
import mimetypes
import random
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

DEFAULT_BASE_URL = "https://trinetra.mahesvara.cloud"
UPLOAD_PATH = "/api/images"
FOLDERS_PATH = "/api/folders"

# Statuses we treat as transient and worth retrying.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class TrinetraError(Exception):
    """Base class for all client errors."""


class TrinetraAuthError(TrinetraError):
    """401 - bad or missing API key."""


class TrinetraValidationError(TrinetraError):
    """400 / 413 - request rejected, retrying will not help."""


class TrinetraRateLimitError(TrinetraError):
    """429 that survived all retry attempts."""


class TrinetraServerError(TrinetraError):
    """5xx that survived all retry attempts."""


@dataclass
class RetryConfig:
    """Tunables for the backoff loop."""

    max_attempts: int = 6          # total tries, including the first
    base_delay: float = 1.0        # seconds, growth base for exponential backoff
    max_delay: float = 60.0        # per-sleep cap, seconds
    max_retry_after: float = 300.0  # never obey a Retry-After larger than this
    jitter: bool = True            # full-jitter on computed (non-header) delays
    proactive_throttle: bool = True  # sleep when X-RateLimit-Remaining hits 0
    max_proactive_delay: float = 10.0  # cap for the success-path throttle sleep


@dataclass
class UploadResult:
    """Parsed successful upload."""

    data: Dict[str, Any]
    status: int
    attempts: int
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return str(self.data.get("url", ""))

    @property
    def thumb_url(self) -> str:
        return str(self.data.get("thumb_url", "") or "")

    @property
    def image_id(self) -> str:
        return str(self.data.get("id", ""))


SleepFn = Callable[[float], None]
LogFn = Callable[[str], None]


def _guess_filename_and_type(filename: Optional[str], mime: Optional[str]) -> Tuple[str, str]:
    """Return a (filename, content_type) pair, filling in sane defaults."""
    name = filename or f"comfyui-{uuid.uuid4().hex}.png"
    ctype = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return name, ctype


def _encode_multipart(
    file_bytes: bytes,
    filename: str,
    content_type: str,
    fields: Dict[str, str],
) -> Tuple[bytes, str]:
    """Build a multipart/form-data body. Returns (body, boundary_content_type)."""
    boundary = f"----ComfyUITrinetra{uuid.uuid4().hex}"
    crlf = b"\r\n"
    buf = io.BytesIO()

    safe_filename = _sanitize_field(filename)
    for key, value in fields.items():
        if value is None or value == "":
            continue
        buf.write(b"--" + boundary.encode() + crlf)
        buf.write(f'Content-Disposition: form-data; name="{key}"'.encode() + crlf)
        buf.write(crlf)
        # Strip CR/LF so a field value cannot inject extra multipart parts.
        buf.write(_sanitize_field(str(value)).encode("utf-8") + crlf)

    buf.write(b"--" + boundary.encode() + crlf)
    buf.write(
        f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"'.encode()
        + crlf
    )
    buf.write(f"Content-Type: {content_type}".encode() + crlf)
    buf.write(crlf)
    buf.write(file_bytes + crlf)
    buf.write(b"--" + boundary.encode() + b"--" + crlf)

    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def _sanitize_field(value: str) -> str:
    r"""Remove CR/LF (and NUL) so a value can't break out of its multipart part.

    Field values and the code-generated filename are the only interpolated
    strings in the body; stripping control chars closes any injection vector.
    """
    return value.replace("\r", "").replace("\n", "").replace("\x00", "")


def _auth_headers(api_key: str, scheme: str) -> Dict[str, str]:
    """Build the auth header for the chosen scheme ('bearer' or 'x-api-key').

    HTTP header values are Latin-1 encoded by http.client, so a key containing a
    non-Latin-1 char (e.g. a smart quote pasted from rich text) would raise a raw
    ``UnicodeEncodeError`` deep in urllib. We reject it up front with a clean
    :class:`TrinetraAuthError` instead.
    """
    key = api_key.strip()
    try:
        key.encode("latin-1")
    except UnicodeEncodeError:
        raise TrinetraAuthError(
            "API key contains non-Latin-1 characters (e.g. a smart quote or "
            "unicode whitespace). Re-copy the key as plain text."
        )
    if scheme == "x-api-key":
        return {"X-API-Key": key}
    # default: bearer
    return {"Authorization": f"Bearer {key}"}


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header. Supports delta-seconds; ignores HTTP-dates.

    RFC 7231 permits only a non-negative integer (delta-seconds) or an HTTP-date.
    We accept the integer form and reject everything else (HTTP-dates, and
    ``float()``-parseable junk like ``inf`` / ``Infinity`` / ``1e400`` / ``nan``
    that would otherwise force a pathological max-length sleep), returning None
    so the caller falls back to exponential backoff.
    """
    if not value:
        return None
    value = value.strip()
    # Strict integer delta-seconds only (optionally signed). Digits guarantee a
    # finite value, sidestepping inf/nan/scientific-notation surprises.
    if value.lstrip("+-").isdigit():
        try:
            return max(0.0, float(int(value)))
        except ValueError:
            return None
    return None


def _compute_backoff(attempt: int, cfg: RetryConfig) -> float:
    """Exponential backoff with optional full jitter. `attempt` is 1-based."""
    raw = cfg.base_delay * (2 ** (attempt - 1))
    raw = min(raw, cfg.max_delay)
    if cfg.jitter:
        # Full jitter: random in [0, raw]. Avoids thundering-herd sync.
        return random.uniform(0.0, raw)
    return raw


def _maybe_proactive_sleep(
    headers: Dict[str, str], cfg: RetryConfig, sleep: SleepFn, log: LogFn
) -> None:
    """If we just used the last rate-limit token, wait for the window reset.

    Reads ``X-RateLimit-Remaining`` and ``X-RateLimit-Reset``. ``Reset`` is
    interpreted as seconds-until-reset if small, or an absolute unix timestamp
    if it looks like one. Best-effort; never raises.
    """
    if not cfg.proactive_throttle:
        return
    remaining_raw = headers.get("x-ratelimit-remaining")
    if remaining_raw is None:
        return
    try:
        remaining = int(float(remaining_raw))
    except (TypeError, ValueError):
        return
    if remaining > 0:
        return

    reset_raw = headers.get("x-ratelimit-reset")
    delay = 1.0
    if reset_raw is not None:
        try:
            reset_val = float(reset_raw)
            now = time.time()
            # Heuristic: values well above "now-ish" are absolute timestamps.
            delay = reset_val - now if reset_val > 1_000_000_000 else reset_val
        except (TypeError, ValueError):
            delay = 1.0
    # Bound by a dedicated, smaller cap so a misbehaving server that pins
    # remaining=0 on every 2xx can't turn a big batch into a multi-minute hang.
    delay = max(0.0, min(delay, cfg.max_proactive_delay))
    if delay > 0:
        log(f"[Trinetra] rate-limit budget exhausted, sleeping {delay:.1f}s for reset")
        sleep(delay)


def _lower_headers(raw: Any) -> Dict[str, str]:
    """Normalize a response's headers into a lowercase-keyed dict."""
    out: Dict[str, str] = {}
    try:
        for k, v in raw.items():
            out[k.lower()] = v
    except AttributeError:
        pass
    return out


def _read_error_message(body: bytes) -> str:
    """Best-effort extraction of a human message from an error body."""
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return body.decode("utf-8", "replace")[:500] if body else ""
    if isinstance(payload, dict):
        for key in ("message", "error", "detail", "title"):
            if key in payload and payload[key]:
                return str(payload[key])
    return json.dumps(payload)[:500]


def upload_image(
    file_bytes: bytes,
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    filename: Optional[str] = None,
    mime: Optional[str] = None,
    folder: Optional[int] = None,
    ttl: Optional[str] = None,
    auth_scheme: str = "bearer",
    timeout: float = 60.0,
    retry: Optional[RetryConfig] = None,
    sleep: SleepFn = time.sleep,
    log: Optional[LogFn] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> UploadResult:
    """Upload one image to Trinetra, retrying transient failures with backoff.

    Parameters
    ----------
    file_bytes:
        Encoded image bytes (PNG/JPEG/GIF/WebP/AVIF).
    api_key:
        Trinetra API key (``tri_...``).
    base_url:
        Instance base URL. Defaults to the public instance.
    filename, mime:
        Optional metadata for the multipart part.
    folder, ttl:
        Optional upload options mapped to the API's ``folder`` / ``ttl`` fields.
    auth_scheme:
        ``"bearer"`` (default) or ``"x-api-key"``.
    retry:
        A :class:`RetryConfig`; defaults are conservative and polite.
    sleep, opener:
        Injection points for testing.

    Returns
    -------
    UploadResult

    Raises
    ------
    TrinetraAuthError, TrinetraValidationError, TrinetraRateLimitError,
    TrinetraServerError, TrinetraError
    """
    if not api_key or not api_key.strip():
        raise TrinetraAuthError("No API key provided. Set your tri_... key on the node.")

    cfg = retry or RetryConfig()
    log = log or (lambda _msg: None)
    do_request = opener or _default_opener

    name, ctype = _guess_filename_and_type(filename, mime)
    extra_fields: Dict[str, str] = {}
    if folder is not None and folder >= 0:
        extra_fields["folder"] = str(folder)
    if ttl:
        extra_fields["ttl"] = ttl

    body, content_type = _encode_multipart(file_bytes, name, ctype, extra_fields)
    url = base_url.rstrip("/") + UPLOAD_PATH

    # A non-ASCII base_url (e.g. an IDN or a stray unicode char) would raise a
    # raw UnicodeEncodeError deep inside urllib. Retrying can't help, so reject
    # it up front with a clean, actionable error.
    try:
        url.encode("ascii")
    except UnicodeEncodeError:
        raise TrinetraValidationError(
            f"base_url contains non-ASCII characters: {base_url!r}. "
            "Use a plain ASCII/punycode URL."
        )

    headers = {"Content-Type": content_type, "Accept": "application/json"}
    headers.update(_auth_headers(api_key, auth_scheme))

    last_exc: Optional[Exception] = None

    for attempt in range(1, cfg.max_attempts + 1):
        try:
            status, resp_body, resp_headers = do_request(url, body, headers, timeout)
        except urllib.error.HTTPError as exc:  # non-2xx surfaces here
            status = exc.code
            resp_body = exc.read() if hasattr(exc, "read") else b""
            resp_headers = _lower_headers(getattr(exc, "headers", {}) or {})
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Network-level hiccup: treat like a transient error and back off.
            last_exc = exc
            if attempt >= cfg.max_attempts:
                raise TrinetraServerError(
                    f"Network error after {attempt} attempts: {exc}"
                ) from exc
            delay = _compute_backoff(attempt, cfg)
            log(f"[Trinetra] network error ({exc}); retry {attempt}/{cfg.max_attempts - 1} in {delay:.1f}s")
            sleep(delay)
            continue

        # --- Success ---------------------------------------------------------
        if 200 <= status < 300:
            try:
                data = json.loads(resp_body.decode("utf-8", "replace")) if resp_body else {}
            except ValueError as exc:
                raise TrinetraError(f"Upload succeeded ({status}) but response was not JSON: {exc}")
            if not isinstance(data, dict):
                raise TrinetraError(f"Unexpected response payload type: {type(data).__name__}")
            _maybe_proactive_sleep(resp_headers, cfg, sleep, log)
            return UploadResult(data=data, status=status, attempts=attempt, headers=resp_headers)

        # --- Non-retryable client errors ------------------------------------
        msg = _read_error_message(resp_body) or f"HTTP {status}"
        if status == 401:
            raise TrinetraAuthError(f"401 Unauthorized: {msg}")
        if status in (400, 413) or (400 <= status < 500 and status != 429):
            # 4xx other than 429 will not improve on retry.
            raise TrinetraValidationError(f"HTTP {status}: {msg}")

        # --- Retryable: 429 and 5xx -----------------------------------------
        if status in RETRYABLE_STATUSES:
            if attempt >= cfg.max_attempts:
                if status == 429:
                    raise TrinetraRateLimitError(
                        f"Still rate-limited (429) after {attempt} attempts: {msg}"
                    )
                raise TrinetraServerError(f"HTTP {status} after {attempt} attempts: {msg}")

            retry_after = _parse_retry_after(resp_headers.get("retry-after"))
            if retry_after is not None:
                delay = min(retry_after, cfg.max_retry_after)
                source = "Retry-After"
            else:
                delay = _compute_backoff(attempt, cfg)
                source = "backoff"
            log(
                f"[Trinetra] HTTP {status}; honoring {source}, "
                f"retry {attempt}/{cfg.max_attempts - 1} in {delay:.1f}s"
            )
            sleep(delay)
            continue

        # --- Anything else: don't loop forever ------------------------------
        raise TrinetraError(f"Unexpected HTTP {status}: {msg}")

    # Loop exhausted without returning or raising a specific error.
    raise TrinetraServerError(
        f"Upload failed after {cfg.max_attempts} attempts"
        + (f": {last_exc}" if last_exc else "")
    )


def _default_opener(
    url: str, body: bytes, headers: Dict[str, str], timeout: float
) -> Tuple[int, bytes, Dict[str, str]]:
    """Perform the actual HTTP POST using urllib. Returns (status, body, headers)."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(), _lower_headers(resp.headers)


def list_folders(
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    auth_scheme: str = "bearer",
    timeout: float = 20.0,
) -> list:
    """Return the account's folders as a list of dicts (id, name, parent_id, ...).

    Calls ``GET /api/folders``. Raises the same Trinetra* errors as
    :func:`upload_image` on auth/validation/rate-limit failures. This is a
    single best-effort GET (no retry loop) intended for populating the folder
    picker; the caller decides how to surface errors.
    """
    if not api_key or not api_key.strip():
        raise TrinetraAuthError("No API key provided.")

    url = base_url.rstrip("/") + FOLDERS_PATH
    try:
        url.encode("ascii")
    except UnicodeEncodeError:
        raise TrinetraValidationError(f"base_url contains non-ASCII characters: {base_url!r}")

    headers = {"Accept": "application/json"}
    headers.update(_auth_headers(api_key, auth_scheme))

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read() if hasattr(exc, "read") else b""
        msg = _read_error_message(raw) or f"HTTP {status}"
        if status == 401:
            raise TrinetraAuthError(f"401 Unauthorized: {msg}")
        if status == 429:
            raise TrinetraRateLimitError(f"429 rate limited: {msg}")
        if 400 <= status < 500:
            raise TrinetraValidationError(f"HTTP {status}: {msg}")
        raise TrinetraServerError(f"HTTP {status}: {msg}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TrinetraServerError(f"Network error listing folders: {exc}") from exc

    try:
        payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError as exc:
        raise TrinetraError(f"Folder list response was not JSON: {exc}")

    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    return [f for f in items if isinstance(f, dict) and "id" in f]
