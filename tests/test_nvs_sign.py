"""Lock the NVS signature algorithm in place.

The signature chain is reverse-engineered from the web bundle. If aiohttp,
hashlib, or our wrapper changes shape and breaks the chain, every cloud call
fails with HTTP 403 and the bridge can't even reach device-list. Locking known
inputs to a known output catches that immediately.
"""
import hashlib
import hmac

from birdfy_api import _hmac_sha256_hex, _md5, _nvs_sign, _redact_response


def _reference_sign(token, ucid, udid, userid, ts):
    s = hmac.new(("nvs1" + token).encode(), ucid.encode(), hashlib.sha256).hexdigest()
    s = hmac.new(s.encode(), udid.encode(), hashlib.sha256).hexdigest()
    s = hmac.new(s.encode(), userid.encode(), hashlib.sha256).hexdigest()
    s = hmac.new(s.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return hmac.new(s.encode(), b"nvs1_request", hashlib.sha256).hexdigest()


def test_nvs_sign_matches_reference():
    sig = _nvs_sign(
        token="t0k3n_example_value",
        ucid="513774810c",
        udid="deadbeefdeadbeefdeadbeefdeadbeef",
        userid="42",
        timestamp="1704067200000",
    )
    expected = _reference_sign(
        "t0k3n_example_value",
        "513774810c",
        "deadbeefdeadbeefdeadbeefdeadbeef",
        "42",
        "1704067200000",
    )
    assert sig == expected
    assert len(sig) == 64
    int(sig, 16)  # must parse as hex


def test_nvs_sign_changes_with_each_input():
    base = dict(
        token="t",
        ucid="u",
        udid="d",
        userid="user",
        timestamp="123",
    )
    base_sig = _nvs_sign(**base)
    for field, new in [
        ("token", "t2"),
        ("ucid", "u2"),
        ("udid", "d2"),
        ("userid", "user2"),
        ("timestamp", "124"),
    ]:
        mutated = dict(base, **{field: new})
        assert _nvs_sign(**mutated) != base_sig, f"sig didn't change when {field} did"


def test_nvs_sign_empty_token_branch():
    # Login uses an empty token — must still produce a deterministic 64-hex digest.
    sig = _nvs_sign(token="", ucid="513774810c", udid="x", userid="", timestamp="1")
    assert len(sig) == 64


def test_hmac_helper():
    assert _hmac_sha256_hex("key", "data") == hmac.new(
        b"key", b"data", hashlib.sha256
    ).hexdigest()


def test_md5_known_vector():
    # "abc" → 900150983cd24fb0d6963f7d28e17f72 — covers the password hashing path.
    assert _md5("abc") == "900150983cd24fb0d6963f7d28e17f72"


def test_redact_strips_token_fields():
    body = '{"data":{"token":"SECRET","userID":42,"region":"us-east-1"}}'
    out = _redact_response(body)
    assert "SECRET" not in out
    assert "REDACTED" in out
    assert "us-east-1" in out  # non-secret fields preserved


def test_redact_strips_nested_credentials():
    body = (
        '{"data":{"credential":{"accessKey":"AKIA...","secretKey":"hunter2",'
        '"sessionToken":"long.thing"},"region":"us-east-1"}}'
    )
    out = _redact_response(body)
    for leaked in ("AKIA", "hunter2", "long.thing"):
        assert leaked not in out, f"{leaked!r} leaked through redaction"


def test_redact_non_json_does_not_dump_body():
    out = _redact_response("<html>error page with token=abc123</token></html>")
    assert "abc123" not in out
    assert "non-JSON" in out
