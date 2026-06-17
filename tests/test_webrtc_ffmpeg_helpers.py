"""Tests for the decode-fallback ffmpeg helpers in webrtc_client.

These belong to the _stream_video fallback path (used only when the jitter-buffer
forwarder isn't taking the stream): the libx264 re-encode command builder, the
process killer, and the track drain. The big connect_and_stream coroutine needs a
live WS + camera and is not unit-tested.
"""
import asyncio
import subprocess

import pytest

pytest.importorskip("aiortc", reason="webrtc_client imports aiortc at module load")

import webrtc_client as wc  # noqa: E402

# --- _start_ffmpeg (re-encode fallback) -----------------------------------

class _FakePopen:
    last_cmd = None

    def __init__(self, cmd, **kw):
        type(self).last_cmd = cmd
        self.stdin = object()


@pytest.fixture
def capture_popen(monkeypatch):
    _FakePopen.last_cmd = None
    monkeypatch.setattr(wc.subprocess, "Popen", _FakePopen)
    return _FakePopen


def test_fallback_ffmpeg_reencodes_to_rtsp(capture_popen):
    wc._start_ffmpeg(640, 480, "rtsp://localhost:8554/birdfy")
    cmd = capture_popen.last_cmd
    # Raw video in, libx264 out (this is the decode/re-encode path).
    assert cmd[cmd.index("-f") + 1] == "rawvideo"
    assert cmd[cmd.index("-s") + 1] == "640x480"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    # zerolatency tuning and RTSP/TCP output.
    assert "zerolatency" in cmd
    assert cmd[cmd.index("-rtsp_transport") + 1] == "tcp"
    assert cmd[-1] == "rtsp://localhost:8554/birdfy"


# --- _kill_ffmpeg ---------------------------------------------------------

class _FakeStdin:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, alive=True):
        self.stdin = _FakeStdin()
        self._alive = alive
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def test_kill_ffmpeg_terminates_live_proc_and_clears_state():
    proc = _FakeProc(alive=True)
    state = {"proc": proc, "size": (640, 480)}
    wc._kill_ffmpeg(state)
    assert proc.stdin.closed
    assert proc.terminated
    assert state["proc"] is None
    assert state["size"] is None


def test_kill_ffmpeg_no_proc_is_safe():
    state = {"proc": None, "size": None}
    wc._kill_ffmpeg(state)  # must not raise
    assert state["proc"] is None


def test_kill_ffmpeg_kills_on_timeout():
    proc = _FakeProc(alive=True)

    def _wait(timeout=None):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)

    proc.wait = _wait
    state = {"proc": proc}
    wc._kill_ffmpeg(state)
    assert proc.killed


# --- _drain ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_consumes_until_track_raises():
    class _Track:
        def __init__(self, n):
            self.n = n
            self.recvs = 0

        async def recv(self):
            self.recvs += 1
            if self.recvs > self.n:
                raise RuntimeError("track ended")
            return b"frame"

    track = _Track(3)
    # _drain swallows the exception and returns cleanly.
    await asyncio.wait_for(wc._drain(track), timeout=1.0)
    assert track.recvs == 4  # 3 frames + the one that raised


# --- _stream_video --------------------------------------------------------

class _Frame:
    def __init__(self, w, h):
        self.width = w
        self.height = h

    def to_ndarray(self, format):  # noqa: A002 - matches av API
        import numpy as np
        return np.zeros((self.height * 3 // 2, self.width), dtype=np.uint8)


class _StreamProc:
    def __init__(self):
        self.stdin = _StdinWriter()

    def poll(self):
        return None  # alive


class _StdinWriter:
    def __init__(self):
        self.writes = 0

    def write(self, data):
        self.writes += 1


@pytest.mark.asyncio
async def test_stream_video_skips_zero_dim_then_starts_ffmpeg(monkeypatch):
    # First frame has 0x0 dims (no keyframe yet) and must be skipped; the second
    # has real dims and must start ffmpeg and write a frame, then the track ends.
    frames = [_Frame(0, 0), _Frame(320, 240)]

    class _Track:
        async def recv(self):
            if frames:
                return frames.pop(0)
            raise RuntimeError("end of stream")

    started = []

    def fake_start(w, h, out):
        started.append((w, h, out))
        return _StreamProc()

    monkeypatch.setattr(wc, "_start_ffmpeg", fake_start)
    monkeypatch.setattr(wc, "_kill_ffmpeg", lambda state: None)

    state = {"proc": None, "size": None}
    await asyncio.wait_for(
        wc._stream_video(_Track(), "rtsp://localhost/birdfy", state, pc=None),
        timeout=2.0,
    )
    # ffmpeg started exactly once at the real resolution.
    assert started == [(320, 240, "rtsp://localhost/birdfy")]


@pytest.mark.asyncio
async def test_stream_video_breaks_on_frame_timeout(monkeypatch):
    monkeypatch.setattr(wc, "FRAME_TIMEOUT", 0.05)
    monkeypatch.setattr(wc, "_kill_ffmpeg", lambda state: None)

    class _StalledTrack:
        async def recv(self):
            await asyncio.sleep(10)  # never returns -> wait_for times out

    state = {"proc": None, "size": None}
    # Should return (break out) shortly after FRAME_TIMEOUT, not hang.
    await asyncio.wait_for(
        wc._stream_video(_StalledTrack(), "rtsp://localhost/birdfy", state, pc=None),
        timeout=2.0,
    )


# --- _pli_nudger ----------------------------------------------------------

@pytest.mark.asyncio
async def test_pli_nudger_sends_pli_then_cancels(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)  # collapse the nudger's 0.5s/2s waits

    monkeypatch.setattr(wc.asyncio, "sleep", fast_sleep)

    sent = {"pli": 0}

    class _Receiver:
        _RTCRtpReceiver__active_ssrc = {1234: 0.0}
        _RTCRtpReceiver__rtcp_ssrc = None  # skip the FIR branch for simplicity

        async def _send_rtcp_pli(self, ssrc):
            sent["pli"] += 1

    class _Transceiver:
        kind = "video"
        receiver = _Receiver()

    class _PC:
        def getTransceivers(self):
            return [_Transceiver()]

    task = asyncio.ensure_future(wc._pli_nudger(_PC()))
    # Let it run a couple of iterations, then cancel.
    for _ in range(20):
        await asyncio.sleep(0)
        if sent["pli"] >= 1:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sent["pli"] >= 1
