"""Tests for the SDP rewrites applied to every offer.

These patches are the breakthrough fixes from the Wireshark debugging session;
if anyone "cleans them up" without re-reading the comments, these tests fail
and explain why.
"""
from _sdp_patches import (
    apply_offer_patches,
    extract_trickle_candidates,
    inject_sctpmap,
    strip_non_sha256_fingerprints,
)

_SAMPLE_OFFER = (
    "v=0\r\n"
    "o=- 1234 2 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0 1 2\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "a=mid:0\r\n"
    "a=ice-ufrag:abcdEF\r\n"
    "a=ice-pwd:ICEPASSWORDxxxxxxxxxxxxxx\r\n"
    "a=fingerprint:sha-256 AA:BB:CC:DD\r\n"
    "a=fingerprint:sha-384 11:22:33\r\n"
    "a=fingerprint:sha-512 44:55:66\r\n"
    "a=candidate:1 1 udp 2122260223 192.168.1.10 54321 typ host\r\n"
    "a=candidate:2 1 udp 1686052607 203.0.113.1 12345 typ srflx\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    "a=mid:1\r\n"
    "a=candidate:3 1 udp 2122260223 192.168.1.10 54322 typ host\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    "a=mid:2\r\n"
    "a=sctp-port:5000\r\n"
)


def test_inject_sctpmap_adds_line_when_missing():
    patched, changed = inject_sctpmap(_SAMPLE_OFFER)
    assert changed is True
    assert "a=sctpmap:5000 webrtc-datachannel 1024" in patched
    # Must appear immediately after a=sctp-port:5000.
    idx_port = patched.index("a=sctp-port:5000")
    idx_map = patched.index("a=sctpmap:5000")
    assert idx_map > idx_port
    assert "a=sctp-port:5000\r\na=sctpmap:5000" in patched


def test_inject_sctpmap_idempotent():
    once, _ = inject_sctpmap(_SAMPLE_OFFER)
    twice, changed = inject_sctpmap(once)
    assert changed is False
    assert once == twice


def test_inject_sctpmap_skips_when_no_sctp_port():
    sdp = "v=0\r\nm=audio 9 RTP/AVP 0\r\n"
    out, changed = inject_sctpmap(sdp)
    assert changed is False
    assert out == sdp


def test_strip_non_sha256_fingerprints_removes_only_384_and_512():
    patched, removed = strip_non_sha256_fingerprints(_SAMPLE_OFFER)
    assert removed == 2
    assert "a=fingerprint:sha-256 AA:BB:CC:DD" in patched
    assert "a=fingerprint:sha-384" not in patched
    assert "a=fingerprint:sha-512" not in patched


def test_strip_fingerprints_noop_when_only_sha256():
    sdp = "a=fingerprint:sha-256 AA:BB\r\n"
    out, removed = strip_non_sha256_fingerprints(sdp)
    assert removed == 0
    assert out == sdp


def test_apply_offer_patches_combines_both():
    patched, info = apply_offer_patches(_SAMPLE_OFFER)
    assert info["sctpmap_injected"] is True
    assert info["fingerprints_stripped"] == 2
    assert "a=sctpmap:5000" in patched
    assert "sha-384" not in patched
    assert "sha-512" not in patched
    # sha-256 fingerprint survives — DTLS would break without it.
    assert "a=fingerprint:sha-256" in patched


def test_extract_trickle_candidates_only_returns_mline_zero():
    cands = extract_trickle_candidates(_SAMPLE_OFFER)
    # Only the two audio (m-line 0) candidates; video m-line 1 candidate skipped.
    assert len(cands) == 2
    for c in cands:
        assert c["sdpMLineIndex"] == 0
        assert c["sdpMid"] == "0"
        assert c["ufrag"] == "abcdEF"
        assert c["candidate"].startswith("candidate:")
        assert " typ " in c["candidate"]


def test_extract_trickle_candidates_strips_a_prefix():
    cands = extract_trickle_candidates(_SAMPLE_OFFER)
    # Browser format starts with "candidate:" not "a=candidate:".
    for c in cands:
        assert not c["candidate"].startswith("a=")
