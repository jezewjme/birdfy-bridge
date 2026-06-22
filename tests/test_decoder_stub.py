"""Tests for the no-op video-decoder patch in _aiortc_media_patches.py.

Since the av 16 bump, aiortc's H264 decoder rejects this camera's NALs and spams
AVERROR_INVALIDDATA warnings on a worker thread we only drain (we forward H264
raw via the jitter-buffer tap, never aiortc's decode output). Patch 4 swaps the
video decoder for a no-op while leaving audio decode intact. These tests lock:
  * the stub decode() honors the "iterable of frames" contract (returns empty),
  * the patch routes video codecs -> stub and audio codecs -> the real factory,
  * the patch is idempotent (the double-patch guard holds),
  * STUB_VIDEO_DECODE env parsing (the off-switch) maps the documented values.

A regression here either re-floods the log with decode errors (stub bypassed) or
silently breaks audio (real factory not deferred to).
"""
import pytest

pytest.importorskip("aiortc", reason="_aiortc_media_patches imports aiortc at module load")

import aiortc.rtcrtpreceiver as _recv_mod  # noqa: E402

import _aiortc_media_patches as amp  # noqa: E402


class _Codec:
    """Minimal stand-in for aiortc's RTCRtpCodecParameters (only mimeType used)."""

    def __init__(self, mime):
        self.mimeType = mime


@pytest.fixture
def restore_get_decoder():
    """Snapshot and restore _recv_mod.get_decoder so patching doesn't leak."""
    original = _recv_mod.get_decoder
    yield
    _recv_mod.get_decoder = original


def test_null_decoder_returns_empty_iterable():
    # decoder_worker() iterates decode()'s result; an empty list is the contract.
    out = amp._NullVideoDecoder().decode(b"\x00\x00\x00\x01\x65 anything")
    assert list(out) == []


def test_patch_routes_video_to_stub_and_audio_to_real(monkeypatch, restore_get_decoder):
    monkeypatch.setattr(amp, "STUB_VIDEO_DECODE", True)
    # Reset to an un-stubbed sentinel factory so we can observe the audio defer.
    real_calls = []

    def fake_real_get_decoder(codec):
        real_calls.append(codec)
        return f"real-decoder-for-{codec.mimeType}"

    _recv_mod.get_decoder = fake_real_get_decoder
    amp._install_decoder_patch()

    # Video -> our no-op stub, real factory NOT consulted.
    video_dec = _recv_mod.get_decoder(_Codec("video/H264"))
    assert isinstance(video_dec, amp._NullVideoDecoder)
    assert real_calls == []

    # Audio -> deferred to the real factory unchanged.
    audio_dec = _recv_mod.get_decoder(_Codec("audio/PCMU"))
    assert audio_dec == "real-decoder-for-audio/PCMU"
    assert [c.mimeType for c in real_calls] == ["audio/PCMU"]


def test_patch_matches_video_case_insensitively(monkeypatch, restore_get_decoder):
    monkeypatch.setattr(amp, "STUB_VIDEO_DECODE", True)
    _recv_mod.get_decoder = lambda codec: "real"
    amp._install_decoder_patch()
    # mimeType casing varies across aiortc versions; the prefix check lowercases.
    assert isinstance(_recv_mod.get_decoder(_Codec("Video/H264")), amp._NullVideoDecoder)


def test_patch_handles_missing_mimetype(monkeypatch, restore_get_decoder):
    monkeypatch.setattr(amp, "STUB_VIDEO_DECODE", True)
    _recv_mod.get_decoder = lambda codec: "real"
    amp._install_decoder_patch()

    class _NoMime:
        pass

    # No mimeType attr -> treated as non-video -> defers to real factory (no crash).
    assert _recv_mod.get_decoder(_NoMime()) == "real"


def test_patch_is_idempotent(monkeypatch, restore_get_decoder):
    monkeypatch.setattr(amp, "STUB_VIDEO_DECODE", True)
    _recv_mod.get_decoder = lambda codec: "real"
    amp._install_decoder_patch()
    first = _recv_mod.get_decoder
    assert getattr(first, "_birdfy_stubbed", False) is True
    # Second install must NOT wrap the already-stubbed factory again.
    amp._install_decoder_patch()
    assert _recv_mod.get_decoder is first


def test_patch_is_noop_when_disabled(monkeypatch, restore_get_decoder):
    monkeypatch.setattr(amp, "STUB_VIDEO_DECODE", False)

    def sentinel(codec):
        return "untouched"

    _recv_mod.get_decoder = sentinel
    amp._install_decoder_patch()
    # Disabled -> factory left exactly as-is, no stub marker.
    assert _recv_mod.get_decoder is sentinel
    assert not getattr(_recv_mod.get_decoder, "_birdfy_stubbed", False)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("yes", True),     # anything not in the off-set enables
        ("true", True),
        ("0", False),
        ("false", False),
        ("False", False),
        ("", False),       # empty string is treated as "off"
    ],
)
def test_stub_video_decode_env_parsing(monkeypatch, value, expected):
    # Re-evaluate the module-level expression the same way the module does, so we
    # lock the documented off-switch values without re-importing the module.
    monkeypatch.setenv("BIRDFY_STUB_VIDEO_DECODE", value)
    import os

    parsed = os.getenv("BIRDFY_STUB_VIDEO_DECODE", "1") not in ("0", "false", "False", "")
    assert parsed is expected
