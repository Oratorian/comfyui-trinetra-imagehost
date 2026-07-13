"""Offline tests for the backoff / error handling in trinetra_client.

Run with:  python -m pytest tests/test_trinetra_client.py
       or: python tests/test_trinetra_client.py

No network access needed: the HTTP opener and sleep function are injected.
"""

import json
import os
import sys

# Package lives one level up (tests/ is a subfolder of the node package root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trinetra_client as tc  # noqa: E402


class FakeClock:
    """Records every sleep() so tests can assert on backoff behavior."""

    def __init__(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)


def make_opener(responses):
    """Return an opener that yields queued (status, body, headers) in order."""
    seq = list(responses)
    calls = {"n": 0}

    def opener(url, body, headers, timeout):
        calls["n"] += 1
        status, payload, hdrs = seq.pop(0)
        raw = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else payload
        return status, raw, {k.lower(): v for k, v in (hdrs or {}).items()}

    opener.calls = calls
    return opener


def _cfg(**kw):
    base = dict(max_attempts=5, base_delay=1.0, max_delay=10.0, jitter=False, proactive_throttle=False)
    base.update(kw)
    return tc.RetryConfig(**base)


def test_success_first_try():
    clock = FakeClock()
    opener = make_opener([(201, {"id": "abc", "url": "https://x/abc.png", "thumb_url": "https://x/t.png"}, {})])
    res = tc.upload_image(b"data", "tri_key", retry=_cfg(), sleep=clock.sleep, opener=opener)
    assert res.url == "https://x/abc.png"
    assert res.image_id == "abc"
    assert res.attempts == 1
    assert clock.slept == []
    print("PASS test_success_first_try")


def test_429_then_success_honors_retry_after():
    clock = FakeClock()
    opener = make_opener([
        (429, {"message": "slow down"}, {"Retry-After": "4"}),
        (201, {"id": "ok", "url": "https://x/ok.png"}, {}),
    ])
    res = tc.upload_image(b"d", "tri_key", retry=_cfg(), sleep=clock.sleep, opener=opener)
    assert res.attempts == 2
    assert res.url == "https://x/ok.png"
    # It must have slept exactly the Retry-After value, not a backoff guess.
    assert clock.slept == [4.0], clock.slept
    print("PASS test_429_then_success_honors_retry_after")


def test_429_without_header_uses_exponential_backoff():
    clock = FakeClock()
    opener = make_opener([
        (429, {"message": "no header"}, {}),
        (429, {"message": "still"}, {}),
        (201, {"id": "z", "url": "https://x/z.png"}, {}),
    ])
    res = tc.upload_image(b"d", "tri_key", retry=_cfg(), sleep=clock.sleep, opener=opener)
    assert res.attempts == 3
    # base_delay=1, no jitter -> 1*2^0=1, then 1*2^1=2
    assert clock.slept == [1.0, 2.0], clock.slept
    print("PASS test_429_without_header_uses_exponential_backoff")


def test_retry_after_is_capped():
    clock = FakeClock()
    opener = make_opener([
        (429, {"message": "huge"}, {"Retry-After": "99999"}),
        (201, {"id": "z", "url": "https://x/z.png"}, {}),
    ])
    res = tc.upload_image(
        b"d", "tri_key", retry=_cfg(max_retry_after=30.0), sleep=clock.sleep, opener=opener
    )
    assert clock.slept == [30.0], clock.slept
    print("PASS test_retry_after_is_capped")


def test_429_exhausts_and_raises_ratelimit():
    clock = FakeClock()
    opener = make_opener([(429, {"message": "nope"}, {"Retry-After": "1"})] * 3)
    try:
        tc.upload_image(b"d", "tri_key", retry=_cfg(max_attempts=3), sleep=clock.sleep, opener=opener)
    except tc.TrinetraRateLimitError as e:
        assert "429" in str(e)
        # 3 attempts -> slept twice (after attempts 1 and 2, not after the last)
        assert clock.slept == [1.0, 1.0], clock.slept
        print("PASS test_429_exhausts_and_raises_ratelimit")
    else:
        raise AssertionError("expected TrinetraRateLimitError")


def test_401_raises_immediately_no_retry():
    clock = FakeClock()
    opener = make_opener([(401, {"message": "bad key"}, {})])
    try:
        tc.upload_image(b"d", "tri_key", retry=_cfg(), sleep=clock.sleep, opener=opener)
    except tc.TrinetraAuthError as e:
        assert "401" in str(e)
        assert clock.slept == []
        assert opener.calls["n"] == 1
        print("PASS test_401_raises_immediately_no_retry")
    else:
        raise AssertionError("expected TrinetraAuthError")


def test_413_raises_validation_no_retry():
    clock = FakeClock()
    opener = make_opener([(413, {"message": "file exceeds max size of 26214400 bytes"}, {})])
    try:
        tc.upload_image(b"d", "tri_key", retry=_cfg(), sleep=clock.sleep, opener=opener)
    except tc.TrinetraValidationError as e:
        assert "413" in str(e)
        assert opener.calls["n"] == 1
        print("PASS test_413_raises_validation_no_retry")
    else:
        raise AssertionError("expected TrinetraValidationError")


def test_5xx_retries_then_succeeds():
    clock = FakeClock()
    opener = make_opener([
        (503, b"service unavailable", {}),
        (201, {"id": "z", "url": "https://x/z.png"}, {}),
    ])
    res = tc.upload_image(b"d", "tri_key", retry=_cfg(), sleep=clock.sleep, opener=opener)
    assert res.attempts == 2
    assert clock.slept == [1.0], clock.slept
    print("PASS test_5xx_retries_then_succeeds")


def test_proactive_throttle_sleeps_when_budget_zero():
    clock = FakeClock()
    opener = make_opener([
        (201, {"id": "z", "url": "https://x/z.png"}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5"}),
    ])
    cfg = _cfg(proactive_throttle=True)
    res = tc.upload_image(b"d", "tri_key", retry=cfg, sleep=clock.sleep, opener=opener)
    assert res.attempts == 1
    assert clock.slept == [5.0], clock.slept
    print("PASS test_proactive_throttle_sleeps_when_budget_zero")


def test_auth_header_schemes():
    captured = {}

    def opener(url, body, headers, timeout):
        captured.update(headers)
        return 201, json.dumps({"id": "z", "url": "https://x/z.png"}).encode(), {}

    tc.upload_image(b"d", "tri_KEY", auth_scheme="bearer", sleep=lambda s: None, opener=opener, retry=_cfg())
    assert captured.get("Authorization") == "Bearer tri_KEY"

    tc.upload_image(b"d", "tri_KEY", auth_scheme="x-api-key", sleep=lambda s: None, opener=opener, retry=_cfg())
    assert captured.get("X-API-Key") == "tri_KEY"
    print("PASS test_auth_header_schemes")


def test_multipart_includes_optional_fields():
    captured = {}

    def opener(url, body, headers, timeout):
        captured["body"] = body
        return 201, json.dumps({"id": "z", "url": "https://x/z.png"}).encode(), {}

    tc.upload_image(
        b"imgdata", "tri_KEY", folder=7, ttl="86400",
        sleep=lambda s: None, opener=opener, retry=_cfg(),
    )
    body = captured["body"].decode("utf-8", "replace")
    assert 'name="folder"' in body and "\r\n7\r\n" in body
    assert 'name="ttl"' in body and "86400" in body
    assert 'name="file"; filename=' in body
    print("PASS test_multipart_includes_optional_fields")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\nAll {len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
