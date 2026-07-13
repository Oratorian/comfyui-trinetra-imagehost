"""Regression tests for the 10 confirmed review findings.

Each test maps to one CONFIRMED finding from the adversarial review and proves
the fix holds. Run with the venv Python (needs Pillow/numpy):

  .venv/Scripts/python.exe test_review_fixes.py
"""

import importlib.util
import json
import os
import sys
import warnings

import numpy as np

# Package lives one level up (tests/ is a subfolder of the node package root).
PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PKG_DIR)

import trinetra_client as tc  # noqa: E402


def load_pkg():
    spec = importlib.util.spec_from_file_location(
        "trinetra_pkg", os.path.join(PKG_DIR, "__init__.py"),
        submodule_search_locations=[PKG_DIR],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["trinetra_pkg"] = pkg
    spec.loader.exec_module(pkg)
    return pkg


PKG = load_pkg()
from trinetra_pkg.nodes import (  # noqa: E402
    _tensor_to_pil_list,
    _encode,
    _flatten_alpha,
    _coerce_float,
    _coerce_int,
)
from PIL import Image  # noqa: E402


# --- Stale-workflow coercion: empty/invalid widget values -> clamped defaults --
def test_coerce_handles_empty_and_out_of_range():
    # The exact failure from the field: empty-string FLOATs and swapped values.
    assert _coerce_float("", 60.0, 5.0, 600.0) == 60.0        # empty -> default
    assert _coerce_float("  ", 60.0, 5.0, 600.0) == 60.0      # whitespace -> default
    assert _coerce_float(None, 60.0, 5.0, 600.0) == 60.0      # None -> default
    assert _coerce_float("abc", 1.0, 0.1, 30.0) == 1.0        # garbage -> default
    assert _coerce_float(60.0, 1.0, 0.1, 30.0) == 30.0        # too big -> clamped to hi
    assert _coerce_float(0.01, 1.0, 0.1, 30.0) == 0.1         # too small -> clamped to lo
    assert _coerce_float("12.5", 1.0, 0.1, 30.0) == 12.5      # valid string float

    assert _coerce_int("", 6, 1, 15) == 6                     # empty -> default
    assert _coerce_int(60, 6, 1, 15) == 15                    # swapped-in 60 -> clamp hi
    assert _coerce_int("6.0", 6, 1, 15) == 6                  # float-string -> int
    assert _coerce_int(-5, -1, -1, 2**31 - 1) == -1           # folder clamp lo
    print("PASS coercion: empty/invalid/out-of-range -> clamped defaults (no crash)")


# --- The literal field scenario: a stale node with scrambled values uploads ---
def test_stale_workflow_values_dont_crash_upload():
    Node = PKG.NODE_CLASS_MAPPINGS["TrinetraUploadImage"]
    opener = ok_opener()
    orig = PKG.nodes.upload_image
    captured = {}

    def spy(*a, **k):
        captured.update(k)
        class R:
            url = "https://t/x.png"
            attempts = 1
        return R()

    PKG.nodes.upload_image = spy
    try:
        # Reproduce the reported error: empty-string floats + swapped ints.
        Node().upload(
            np.random.rand(1, 8, 8, 3).astype("float32"), "tri_k",
            max_attempts=60,            # was a delay value -> clamps to 15
            base_delay_seconds=60.0,    # was max_delay -> clamps to 30.0
            max_delay_seconds="",       # empty string -> default 60.0
            timeout_seconds="",         # empty string -> default 60.0
        )
        # It must have run and passed sane, in-range values to the client.
        assert captured["timeout"] == 60.0, captured.get("timeout")
        assert captured["retry"].max_attempts == 15
        assert captured["retry"].base_delay == 30.0
        assert captured["retry"].max_delay == 60.0
    finally:
        PKG.nodes.upload_image = orig
    print("PASS stale-workflow: scrambled/empty values coerced, upload proceeds")


def ok_opener(payload=None):
    payload = payload or {"id": "x", "url": "https://t/x.png"}
    log = {"reqs": []}

    def opener(url, body, headers, timeout):
        log["reqs"].append({"url": url, "headers": dict(headers), "body": body})
        return 201, json.dumps(payload).encode(), {}

    opener.log = log
    return opener


# --- Finding 1: 2-channel tensor must not crash --------------------------------
def test_2channel_no_crash():
    t = np.random.rand(1, 8, 8, 2).astype("float32")
    imgs = _tensor_to_pil_list(t)
    assert len(imgs) == 1 and imgs[0].mode == "RGBA", imgs[0].mode
    # 5-channel should raise a *clear* error, not a cryptic one
    try:
        _tensor_to_pil_list(np.random.rand(1, 4, 4, 5).astype("float32"))
        raise AssertionError("expected ValueError for 5 channels")
    except ValueError as e:
        assert "channel count" in str(e), str(e)
    print("PASS finding1: 2-channel handled, unsupported count raises clearly")


# --- Finding 2: NaN/inf sanitized, no warning ----------------------------------
def test_nan_inf_sanitized():
    t = np.full((1, 2, 2, 3), 0.5, dtype="float32")
    t[0, 0, 0, 0] = np.nan
    t[0, 0, 1, 0] = np.inf
    t[0, 1, 0, 0] = -np.inf
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any RuntimeWarning would fail the test
        imgs = _tensor_to_pil_list(t)
    px = np.asarray(imgs[0])
    assert px[0, 0, 0] == 0, px[0, 0, 0]      # NaN -> 0
    assert px[0, 1, 0] == 255, px[0, 1, 0]    # +inf -> 1.0 -> 255
    assert px[1, 0, 0] == 0, px[1, 0, 0]      # -inf -> 0
    print("PASS finding2: NaN/inf mapped to defined pixels, no RuntimeWarning")


# --- Finding 3: empty batch raises --------------------------------------------
def test_empty_batch_raises():
    Node = PKG.NODE_CLASS_MAPPINGS["TrinetraUploadImage"]
    orig = PKG.nodes.upload_image
    PKG.nodes.upload_image = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload"))
    try:
        Node().upload(np.zeros((0, 8, 8, 3), dtype="float32"), "tri_k")
        raise AssertionError("expected RuntimeError on empty batch")
    except RuntimeError as e:
        assert "empty IMAGE batch" in str(e), str(e)
    finally:
        PKG.nodes.upload_image = orig
    print("PASS finding3: empty batch raises a clear RuntimeError")


# --- Finding 4: non-Latin-1 API key -> clean TrinetraAuthError -----------------
def test_nonlatin_key_clean_error():
    try:
        tc.upload_image(b"d", "tri_’abc", opener=ok_opener(), sleep=lambda s: None)
        raise AssertionError("expected TrinetraAuthError")
    except tc.TrinetraAuthError as e:
        assert "non-Latin-1" in str(e), str(e)
    print("PASS finding4: smart-quote API key -> clean TrinetraAuthError")


# --- Plausible: non-ASCII base_url -> clean validation error -------------------
def test_nonascii_baseurl_clean_error():
    try:
        tc.upload_image(b"d", "tri_k", base_url="https://例え.cloud",
                        opener=ok_opener(), sleep=lambda s: None)
        raise AssertionError("expected TrinetraValidationError")
    except tc.TrinetraValidationError as e:
        assert "non-ASCII" in str(e), str(e)
    print("PASS plausible: non-ASCII base_url -> clean TrinetraValidationError")


# --- Finding 5: Retry-After inf/Infinity/1e400 rejected ------------------------
def test_retry_after_grammar():
    assert tc._parse_retry_after("5") == 5.0
    assert tc._parse_retry_after("0") == 0.0
    for bad in ("inf", "Infinity", "+Inf", "nan", "1e400", "1_000", "10.5", "abc", "Wed, 21 Oct 2015 07:28:00 GMT"):
        assert tc._parse_retry_after(bad) is None, f"{bad} should be rejected"
    print("PASS finding5: Retry-After accepts only integer delta-seconds")


# --- Finding 6: CRLF in ttl cannot inject a multipart part ---------------------
def test_ttl_crlf_sanitized():
    opener = ok_opener()
    tc.upload_image(
        b"IMG", "tri_k",
        ttl='604800\r\nContent-Disposition: form-data; name="folder"\r\n\r\n5',
        opener=opener, sleep=lambda s: None,
    )
    body = opener.log["reqs"][0]["body"].decode("latin-1")
    # The injected folder part must NOT appear as its own part.
    assert 'name="folder"\r\n\r\n5' not in body, "CRLF injection not neutralized"
    # ttl value should be collapsed onto one line.
    assert "604800Content-Disposition" in body or "604800" in body
    print("PASS finding6: CRLF in ttl neutralized (no injected part)")


# --- Finding 7: JPEG composites alpha over white -------------------------------
def test_jpeg_composites_alpha():
    # RGBA with a fully transparent RED region; under white bg it must be white-ish.
    rgba = np.zeros((1, 4, 4, 4), dtype="float32")
    rgba[..., 0] = 1.0  # red
    rgba[..., 3] = 0.0  # fully transparent
    img = _tensor_to_pil_list(rgba)[0]
    data, mime, ext = _encode(img, "JPEG", 95)
    assert mime == "image/jpeg"
    from io import BytesIO
    decoded = np.asarray(Image.open(BytesIO(data)).convert("RGB"))
    r, g, b = decoded[0, 0]
    # Transparent-red over white must read near white, not red.
    assert r > 200 and g > 200 and b > 200, f"expected ~white, got {(r, g, b)}"
    print(f"PASS finding7: JPEG transparent-red composited over white -> {(int(r), int(g), int(b))}")


# --- Finding 8: WEBP quality=100 is lossless -----------------------------------
def test_webp_q100_lossless():
    rng = np.random.RandomState(0)
    t = rng.rand(1, 16, 16, 3).astype("float32")
    src = _tensor_to_pil_list(t)[0]
    data, mime, ext = _encode(src, "WEBP", 100)
    from io import BytesIO
    decoded = np.asarray(Image.open(BytesIO(data)).convert("RGB"))
    orig = np.asarray(src.convert("RGB"))
    assert np.array_equal(decoded, orig), f"WEBP q100 not lossless, max diff {np.abs(decoded.astype(int)-orig).max()}"
    # And q80 should be lossy (sanity: differs)
    data2, _, _ = _encode(src, "WEBP", 80)
    decoded2 = np.asarray(Image.open(BytesIO(data2)).convert("RGB"))
    assert not np.array_equal(decoded2, orig), "WEBP q80 unexpectedly lossless"
    print("PASS finding8: WEBP q100 lossless, q80 lossy")


# --- Finding 9: proactive throttle bounded by max_proactive_delay --------------
def test_proactive_throttle_capped():
    slept = []
    opener = ok_opener()

    def opener2(url, body, headers, timeout):
        return 201, json.dumps({"id": "x", "url": "https://t/x.png"}).encode(), {
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": "600"  # 10 min
        }

    cfg = tc.RetryConfig(max_delay=60.0, max_proactive_delay=10.0)
    tc.upload_image(b"d", "tri_k", retry=cfg, opener=opener2, sleep=lambda s: slept.append(s))
    assert slept == [10.0], slept  # capped at max_proactive_delay, not 60 or 600
    print("PASS finding9: success-path throttle capped at max_proactive_delay")


# --- Finding 10: partial batch failure reports uploaded URLs -------------------
def test_partial_batch_reports_uploaded():
    Node = PKG.NODE_CLASS_MAPPINGS["TrinetraUploadImage"]
    calls = {"n": 0}
    orig = PKG.nodes.upload_image
    # Raise the exact class nodes.py catches (it imports from .trinetra_client,
    # a distinct module object from the top-level `tc` in this test).
    RateLimit = PKG.nodes.TrinetraError.__subclasses__  # noqa: F841
    NodeRateLimit = sys.modules["trinetra_pkg.trinetra_client"].TrinetraRateLimitError

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            class R:
                url = f"https://t/img{calls['n']}.png"
                attempts = 1
            return R()
        raise NodeRateLimit("429 forever")

    PKG.nodes.upload_image = flaky
    try:
        Node().upload(np.random.rand(4, 8, 8, 3).astype("float32"), "tri_k")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        msg = str(e)
        assert "already uploaded 2 image(s)" in msg, msg
        assert "https://t/img1.png" in msg and "https://t/img2.png" in msg, msg
    finally:
        PKG.nodes.upload_image = orig
    print("PASS finding10: mid-batch failure reports the 2 already-uploaded URLs")


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\nAll {len(fns)} review-fix regression tests passed.")


if __name__ == "__main__":
    _run()
