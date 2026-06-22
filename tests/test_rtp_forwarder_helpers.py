"""Tests for the small sync helpers in _rtp_forwarder not covered elsewhere.

test_rtp_forwarder.py covers _iter_nals / _ParamSetCache / _write_all and
test_ffmpeg_cmd.py covers _start_ffmpeg. Here: the ffmpeg-log tailer, the
process reaper, and the jitter-buffer tap wrap/unwrap (with a fake receiver).
"""
import subprocess

import pytest

pytest.importorskip("aiortc", reason="_rtp_forwarder imports aiortc at module load")

import _rtp_forwarder as rf  # noqa: E402

# --- _log_ffmpeg_tail -----------------------------------------------------

def test_log_ffmpeg_tail_missing_file_is_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(rf, "_FFMPEG_LOG_PATH", str(tmp_path / "nope.log"))
    rf._log_ffmpeg_tail()  # must not raise


def test_log_ffmpeg_tail_emits_last_lines(monkeypatch, tmp_path, caplog):
    log = tmp_path / "ffmpeg.log"
    log.write_text("\n".join(f"line{i}" for i in range(100)), encoding="utf-8")
    monkeypatch.setattr(rf, "_FFMPEG_LOG_PATH", str(log))
    with caplog.at_level("INFO"):
        rf._log_ffmpeg_tail(max_lines=5)
    assert "ffmpeg stderr tail" in caplog.text
    assert "line99" in caplog.text
    assert "line0" not in caplog.text  # only the tail


# --- _reap_ffmpeg ---------------------------------------------------------

def test_reap_none_is_noop():
    rf._reap_ffmpeg(None)  # must not raise


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
        self.waited = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def kill(self):
        self.killed = True


def test_reap_live_process_closes_stdin_and_terminates():
    proc = _FakeProc(alive=True)
    rf._reap_ffmpeg(proc)
    assert proc.stdin.closed
    assert proc.terminated
    assert proc.waited
    assert not proc.killed


def test_reap_already_dead_process_only_closes_stdin():
    proc = _FakeProc(alive=False)
    rf._reap_ffmpeg(proc)
    assert proc.stdin.closed
    assert not proc.terminated  # poll() said it's gone


def test_reap_kills_on_wait_timeout():
    proc = _FakeProc(alive=True)

    def _wait(timeout=None):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)

    proc.wait = _wait
    rf._reap_ffmpeg(proc)
    assert proc.killed


# --- _wrap_jitter_buffer / _unwrap_jitter_buffer --------------------------

class _FakeJitterBuffer:
    """Mimics aiortc JitterBuffer.add: returns (pli_flag, encoded_frame)."""

    def __init__(self, frames):
        # frames: list of encoded_frame values to return on successive add() calls
        self._frames = list(frames)

    def add(self, packet):
        frame = self._frames.pop(0) if self._frames else None
        return (False, frame)


class _FakeReceiver:
    def __init__(self, jb):
        self._RTCRtpReceiver__jitter_buffer = jb


def test_wrap_taps_completed_frames():
    jb = _FakeJitterBuffer(frames=["FRAME_A", None, "FRAME_B"])
    receiver = _FakeReceiver(jb)
    seen = []
    rf._wrap_jitter_buffer(receiver, seen.append)

    jb.add(object())  # -> FRAME_A
    jb.add(object())  # -> None (no callback)
    jb.add(object())  # -> FRAME_B
    assert seen == ["FRAME_A", "FRAME_B"]
    assert getattr(jb, "_birdfy_tapped", False) is True


def test_wrap_is_idempotent():
    jb = _FakeJitterBuffer(frames=["X"])
    receiver = _FakeReceiver(jb)
    seen = []
    rf._wrap_jitter_buffer(receiver, seen.append)
    tapped_add = jb.add
    rf._wrap_jitter_buffer(receiver, seen.append)  # second wrap is a no-op
    assert jb.add is tapped_add


def test_wrap_callback_exception_does_not_break_add():
    jb = _FakeJitterBuffer(frames=["X"])
    receiver = _FakeReceiver(jb)

    def boom(_frame):
        raise ValueError("callback blew up")

    rf._wrap_jitter_buffer(receiver, boom)
    # add() must still return the frame even though the callback raised.
    pli, frame = jb.add(object())
    assert frame == "X"


def test_unwrap_restores_original_add():
    jb = _FakeJitterBuffer(frames=["X"])
    receiver = _FakeReceiver(jb)
    class_add = type(jb).add
    rf._wrap_jitter_buffer(receiver, lambda f: None)
    assert jb.add is not class_add  # instance wrapper in place
    rf._unwrap_jitter_buffer(receiver)
    # The instance attribute is gone, so the class method shows through again.
    assert jb.add.__func__ is class_add
    assert not getattr(jb, "_birdfy_tapped", False)


def test_unwrap_untapped_is_safe():
    jb = _FakeJitterBuffer(frames=[])
    receiver = _FakeReceiver(jb)
    rf._unwrap_jitter_buffer(receiver)  # never tapped — must not raise


def test_wrap_missing_jitter_buffer_raises():
    class _Bare:
        pass

    with pytest.raises(RuntimeError, match="no __jitter_buffer"):
        rf._wrap_jitter_buffer(_Bare(), lambda f: None)
