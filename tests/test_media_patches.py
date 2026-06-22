"""Tests for the buffer/NACK/diagnostic patches in _aiortc_media_patches.

The no-op decoder patch is covered in test_decoder_stub.py. Here we cover the
env parsing, the video-only jitter-buffer widening, the periodic re-NACK
attach/detach lifecycle (with a fake receiver), and the RTCP-feedback logger.
"""
import asyncio

import pytest

pytest.importorskip("aiortc", reason="_aiortc_media_patches imports aiortc at module load")

from aiortc.jitterbuffer import JitterBuffer  # noqa: E402

import _aiortc_media_patches as amp  # noqa: E402

# --- _env_int -------------------------------------------------------------

def test_env_int_default_when_unset(monkeypatch):
    monkeypatch.delenv("X_INT", raising=False)
    assert amp._env_int("X_INT", 99) == 99


def test_env_int_default_when_blank(monkeypatch):
    monkeypatch.setenv("X_INT", "   ")
    assert amp._env_int("X_INT", 7) == 7


def test_env_int_parses_value(monkeypatch):
    monkeypatch.setenv("X_INT", "123")
    assert amp._env_int("X_INT", 0) == 123


def test_env_int_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("X_INT", "notanint")
    assert amp._env_int("X_INT", 42) == 42


# --- jitter-buffer widening patch -----------------------------------------

def test_install_buffer_patches_widens_video_only():
    amp._install_buffer_patches()
    try:
        # Video buffer below the target capacity is widened.
        vid = JitterBuffer(capacity=8, is_video=True)
        assert vid._capacity >= amp.JITTER_CAPACITY
        # Audio buffer is left at its small size (not widened).
        aud = JitterBuffer(capacity=16, is_video=False)
        assert aud._capacity == 16
    finally:
        # Restore the original __init__ so we don't leak the patch into other tests.
        JitterBuffer.__init__ = amp._orig_jb_init


def test_install_buffer_patches_is_idempotent():
    amp._install_buffer_patches()
    patched = JitterBuffer.__init__
    amp._install_buffer_patches()
    try:
        assert JitterBuffer.__init__ is patched
    finally:
        JitterBuffer.__init__ = amp._orig_jb_init


# --- periodic re-NACK lifecycle -------------------------------------------

class _FakeNackGen:
    def __init__(self, missing):
        self.missing = missing


class _FakeReceiver:
    def __init__(self, missing=None, active_ssrc=None):
        # Name-mangled attrs aiortc exposes on RTCRtpReceiver.
        self._RTCRtpReceiver__nack_generator = _FakeNackGen(missing or set())
        self._RTCRtpReceiver__active_ssrc = active_ssrc or {}
        self.nack_calls = []

    async def _send_rtcp_nack(self, ssrc, seqs):
        self.nack_calls.append((ssrc, list(seqs)))


@pytest.mark.asyncio
async def test_attach_renack_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(amp, "NACK_INTERVAL_MS", 0)
    r = _FakeReceiver()
    amp.attach_periodic_renack(r)
    assert id(r) not in amp._renack_tasks


@pytest.mark.asyncio
async def test_attach_renack_skips_when_no_nack_generator(monkeypatch):
    monkeypatch.setattr(amp, "NACK_INTERVAL_MS", 30)

    class _Bare:
        pass

    bare = _Bare()
    amp.attach_periodic_renack(bare)
    assert id(bare) not in amp._renack_tasks


@pytest.mark.asyncio
async def test_attach_then_detach_renack(monkeypatch):
    monkeypatch.setattr(amp, "NACK_INTERVAL_MS", 30)
    r = _FakeReceiver(missing={5, 6}, active_ssrc={1234: 0.0})
    amp.attach_periodic_renack(r)
    assert id(r) in amp._renack_tasks
    task = amp._renack_tasks[id(r)]
    assert not task.done()
    # Idempotent: attaching again must not spawn a second task.
    amp.attach_periodic_renack(r)
    assert amp._renack_tasks[id(r)] is task
    # Detach cancels and removes it.
    amp.detach_periodic_renack(r)
    assert id(r) not in amp._renack_tasks
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_renack_loop_sends_nack_for_missing(monkeypatch):
    # Drive the loop with a near-zero interval and let it run one tick.
    monkeypatch.setattr(amp, "NACK_INTERVAL_MS", 1)
    r = _FakeReceiver(missing={10, 11, 12}, active_ssrc={4321: 0.0})
    amp.attach_periodic_renack(r)
    try:
        # Yield enough times for the loop to wake, read missing, and send a NACK.
        for _ in range(50):
            await asyncio.sleep(0.002)
            if r.nack_calls:
                break
        assert r.nack_calls, "re-NACK loop never sent a NACK for the missing seqs"
        ssrc, seqs = r.nack_calls[0]
        assert ssrc == 4321
        assert set(seqs) == {10, 11, 12}
    finally:
        amp.detach_periodic_renack(r)


def test_detach_renack_unknown_receiver_is_safe():
    # Detaching a receiver that was never attached must not raise.
    amp.detach_periodic_renack(_FakeReceiver())


# --- log_video_rtcp_feedback ----------------------------------------------

class _FB:
    def __init__(self, type_, parameter=""):
        self.type = type_
        self.parameter = parameter


class _Codec:
    def __init__(self, name, fb):
        self.name = name
        self.clockRate = 90000
        self.rtcpFeedback = fb


class _Recv:
    def __init__(self, codecs):
        self._RTCRtpReceiver__codecs = codecs


class _Transceiver:
    def __init__(self, kind, receiver):
        self.kind = kind
        self.receiver = receiver


class _PC:
    def __init__(self, transceivers):
        self._t = transceivers

    def getTransceivers(self):
        return self._t


def test_log_rtcp_feedback_runs_for_video(caplog):
    # A video codec advertising NACK -> the logger reports NACK=YES; this just
    # needs to execute without error and touch the video branch.
    recv = _Recv({96: _Codec("H264", [_FB("nack"), _FB("nack", "pli")])})
    pc = _PC([_Transceiver("audio", None), _Transceiver("video", recv)])
    log_video = amp.log_video_rtcp_feedback
    log_video(pc)  # must not raise; audio transceiver is skipped


def test_log_rtcp_feedback_handles_no_codecs():
    recv = _Recv(None)
    pc = _PC([_Transceiver("video", recv)])
    amp.log_video_rtcp_feedback(pc)  # no negotiated codecs branch, no raise
