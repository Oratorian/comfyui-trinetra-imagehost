"""End-to-end test of the actual ComfyUI node, using real Pillow/numpy.

This imports the node exactly as ComfyUI would (as a package), builds a real
IMAGE tensor (numpy [B,H,W,C] float32 in [0,1]), and drives the node's
`upload()` method. The HTTP layer is mocked at the opener level so no API key
or network is needed, but EVERYTHING else is real: tensor->PIL conversion,
PNG/JPEG/WEBP encoding, multipart assembly, the retry loop, and the node's
`{ui, result}` return contract.

Run:  .venv/Scripts/python.exe test_node_e2e.py
"""

import importlib.util
import json
import os
import sys

import numpy as np

# Package lives one level up (tests/ is a subfolder of the node package root).
PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_package():
    """Load the package root so nodes.py relative imports resolve."""
    spec = importlib.util.spec_from_file_location(
        "trinetra_pkg", os.path.join(PKG_DIR, "__init__.py"),
        submodule_search_locations=[PKG_DIR],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["trinetra_pkg"] = pkg
    spec.loader.exec_module(pkg)
    return pkg


def make_opener(script):
    """Opener yielding queued (status, payload, headers); records requests."""
    seq = list(script)
    log = {"requests": []}

    def opener(url, body, headers, timeout):
        log["requests"].append({"url": url, "headers": dict(headers), "body_len": len(body), "body": body})
        status, payload, hdrs = seq.pop(0)
        raw = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else payload
        return status, raw, {k.lower(): v for k, v in (hdrs or {}).items()}

    opener.log = log
    return opener


def patch_opener(pkg, opener):
    """Force the node's client to use our opener + instant sleep."""
    client = pkg.nodes.upload_image  # function; we wrap via default arg override

    # We can't easily change the default opener, so monkeypatch the module-level
    # _default_opener isn't used when we pass opener explicitly. Instead, wrap
    # upload_image so the node's call picks up our injected opener + sleep.
    orig = pkg.nodes.upload_image

    def wrapped(*args, **kwargs):
        kwargs.setdefault("opener", opener)
        kwargs.setdefault("sleep", lambda s: None)
        return orig(*args, **kwargs)

    pkg.nodes.upload_image = wrapped
    return orig


def rgb_tensor(batch=1, h=16, w=16, channels=3):
    return np.random.rand(batch, h, w, channels).astype("float32")


def run():
    pkg = load_package()
    Node = pkg.NODE_CLASS_MAPPINGS["TrinetraUploadImage"]
    passed = []

    # --- 1. Single image, PNG, happy path -------------------------------------
    opener = make_opener([(201, {"id": "img1", "url": "https://t/img1.png", "thumb_url": "https://t/img1_t.png"}, {})])
    patch_opener(pkg, opener)
    node = Node()
    out = node.upload(rgb_tensor(1), "tri_testkey", format="PNG")
    assert set(out.keys()) == {"ui", "result"}, out.keys()
    first, all_urls, report = out["result"]
    assert first == "https://t/img1.png", first
    assert all_urls == "https://t/img1.png"
    assert "img1.png" in report
    # verify a real PNG was actually encoded and sent
    body = opener.log["requests"][0]["body"]
    assert b"\x89PNG\r\n" in body, "real PNG signature must be in multipart body"
    assert b'name="file"; filename="comfyui_000.png"' in body
    assert out["result"][0].startswith("https://")
    passed.append("single PNG happy path + real PNG bytes in multipart")

    # --- 2. Batch of 3, JPEG, all uploaded ------------------------------------
    opener = make_opener([
        (201, {"id": f"b{i}", "url": f"https://t/b{i}.jpg"}, {}) for i in range(3)
    ])
    patch_opener(pkg, opener)
    out = Node().upload(rgb_tensor(3), "tri_testkey", format="JPEG", quality=70)
    first, all_urls, report = out["result"]
    assert all_urls.split("\n") == ["https://t/b0.jpg", "https://t/b1.jpg", "https://t/b2.jpg"], all_urls
    assert len(opener.log["requests"]) == 3
    # JPEG magic bytes
    assert opener.log["requests"][0]["body"][:200].find(b"\xff\xd8\xff") != -1 or b"\xff\xd8\xff" in opener.log["requests"][0]["body"]
    passed.append("batch of 3 JPEG, 3 uploads, correct url join")

    # --- 3. WEBP encoding path -------------------------------------------------
    opener = make_opener([(201, {"id": "w", "url": "https://t/w.webp"}, {})])
    patch_opener(pkg, opener)
    out = Node().upload(rgb_tensor(1), "tri_testkey", format="WEBP", quality=80)
    assert out["result"][0] == "https://t/w.webp"
    assert b"WEBP" in opener.log["requests"][0]["body"]
    passed.append("WEBP encoding path")

    # --- 4. 429 -> backoff -> success THROUGH THE NODE ------------------------
    opener = make_opener([
        (429, {"message": "slow down"}, {"Retry-After": "2"}),
        (429, {"message": "still"}, {}),
        (201, {"id": "ok", "url": "https://t/ok.png"}, {}),
    ])
    patch_opener(pkg, opener)
    out = Node().upload(rgb_tensor(1), "tri_testkey", format="PNG", max_attempts=5)
    assert out["result"][0] == "https://t/ok.png"
    assert "attempts=3" in out["result"][2], out["result"][2]
    assert len(opener.log["requests"]) == 3
    passed.append("node survives 429x2 then succeeds (attempts=3)")

    # --- 5. Auth header actually applied on the wire --------------------------
    opener = make_opener([(201, {"id": "a", "url": "https://t/a.png"}, {})])
    patch_opener(pkg, opener)
    Node().upload(rgb_tensor(1), "tri_SECRET", auth_scheme="bearer")
    assert opener.log["requests"][0]["headers"].get("Authorization") == "Bearer tri_SECRET"
    opener = make_opener([(201, {"id": "a", "url": "https://t/a.png"}, {})])
    patch_opener(pkg, opener)
    Node().upload(rgb_tensor(1), "tri_SECRET", auth_scheme="x-api-key")
    assert opener.log["requests"][0]["headers"].get("X-API-Key") == "tri_SECRET"
    passed.append("bearer + x-api-key headers correct on the wire")

    # --- 6. folder + expiry (preset -> ttl) mapped into multipart -------------
    opener = make_opener([(201, {"id": "f", "url": "https://t/f.png", "expires_at": 123}, {})])
    patch_opener(pkg, opener)
    Node().upload(rgb_tensor(1), "tri_k", folder=42, expiry="1 day")
    body = opener.log["requests"][0]["body"].decode("latin-1")
    assert 'name="folder"' in body and "42" in body
    assert 'name="ttl"' in body and "86400" in body  # "1 day" preset -> 86400
    passed.append("folder + expiry preset ('1 day' -> ttl 86400) serialized")

    # --- 6b. 'On date...' with a validated at:<ms> flows through --------------
    opener = make_opener([(201, {"id": "d", "url": "https://t/d.png"}, {})])
    patch_opener(pkg, opener)
    Node().upload(rgb_tensor(1), "tri_k", expiry="On date...", ttl_at="at:1893456000000")
    body = opener.log["requests"][0]["body"].decode("latin-1")
    assert 'name="ttl"' in body and "at:1893456000000" in body
    passed.append("'On date...' with at:<ms> serialized into ttl")

    # --- 6c. 'Never' omits ttl entirely --------------------------------------
    opener = make_opener([(201, {"id": "n", "url": "https://t/n.png"}, {})])
    patch_opener(pkg, opener)
    Node().upload(rgb_tensor(1), "tri_k", expiry="Never (permanent)")
    body = opener.log["requests"][0]["body"].decode("latin-1")
    assert 'name="ttl"' not in body, "Never must not send a ttl field"
    passed.append("'Never' omits the ttl field")

    # --- 6d. 'On date...' with a bogus/empty at: value fails BEFORE upload ----
    for bad in ("", "notanumber", "at:", "at:xyz", "at:-5", "86400"):
        opener = make_opener([(201, {"id": "z", "url": "https://t/z.png"}, {})])
        patch_opener(pkg, opener)
        try:
            Node().upload(rgb_tensor(1), "tri_k", expiry="On date...", ttl_at=bad)
            raise AssertionError(f"expected RuntimeError for ttl_at={bad!r}")
        except RuntimeError:
            # Critical: no HTTP request may have been made (upload not spent).
            assert len(opener.log["requests"]) == 0, f"upload was spent on bad ttl_at={bad!r}"
    passed.append("'On date...' with bogus at: rejected BEFORE any upload")

    # --- 7. RGBA input preserved through PNG ----------------------------------
    opener = make_opener([(201, {"id": "rgba", "url": "https://t/rgba.png"}, {})])
    patch_opener(pkg, opener)
    out = Node().upload(rgb_tensor(1, channels=4), "tri_k", format="PNG")
    assert out["result"][0] == "https://t/rgba.png"
    assert b"\x89PNG" in opener.log["requests"][0]["body"]
    passed.append("RGBA (4-channel) input handled")

    # --- 8. 401 surfaces as a clean node error --------------------------------
    opener = make_opener([(401, {"message": "bad key"}, {})])
    patch_opener(pkg, opener)
    try:
        Node().upload(rgb_tensor(1), "tri_wrong")
        raise AssertionError("expected RuntimeError on 401")
    except RuntimeError as e:
        assert "401" in str(e) and "bad key" in str(e), str(e)
    passed.append("401 raised as RuntimeError with server message")

    print("E2E node tests:")
    for i, p in enumerate(passed, 1):
        print(f"  [{i}] PASS  {p}")
    print(f"\nAll {len(passed)} end-to-end node tests passed.")


if __name__ == "__main__":
    run()
