"""Tests for the ffmpeg command-building in _rtp_forwarder._start_ffmpeg().

Timestamp approach: a `setts` bitstream filter assigns constant-rate, monotonic
PTS/DTS on the copied H264 stream starting at PTS 0, in RTP's 90 kHz timebase.
The N*tick value is 90000 // FRAME_RATE, so it MUST track BIRDFY_FRAME_RATE.

The origin-0 PTS matters: we tried -use_wallclock_as_timestamps to share the
audio's real clock, but it stamps video at absolute epoch while audio starts at
0, and the billion-second origin gap made Frigate's reader find no video stream
and crash-loop. So video stays on setts (origin 0); A/V drift is bounded by
keeping FRAME_RATE near the real delivered rate instead.

We capture the argv handed to subprocess.Popen instead of spawning ffmpeg.
"""
import importlib

import pytest

pytest.importorskip("aiortc", reason="_rtp_forwarder imports aiortc at module load")

import _rtp_forwarder  # noqa: E402


class _FakePopen:
    """Captures argv; mimics just enough of Popen for _start_ffmpeg's return."""

    last_cmd = None

    def __init__(self, cmd, **kw):
        type(self).last_cmd = cmd
        self.stdin = None


@pytest.fixture
def capture_popen(monkeypatch):
    _FakePopen.last_cmd = None
    monkeypatch.setattr(_rtp_forwarder.subprocess, "Popen", _FakePopen)
    return _FakePopen


def _setts_arg(cmd):
    """Return the value following -bsf:v in the captured argv."""
    i = cmd.index("-bsf:v")
    return cmd[i + 1]


def test_uses_setts_not_wallclock_genpts_or_fps_mode(capture_popen):
    _rtp_forwarder._start_ffmpeg("rtsp://localhost:8554/birdfy")
    cmd = capture_popen.last_cmd
    # setts present; the rejected knobs absent. -use_wallclock_as_timestamps in
    # particular must NOT be here — its absolute-epoch PTS broke Frigate's reader.
    assert "-bsf:v" in cmd
    assert "-use_wallclock_as_timestamps" not in cmd
    assert "+genpts" not in cmd
    assert "-fps_mode" not in cmd


def test_setts_uses_90khz_timebase_and_default_rate(capture_popen):
    # Default FRAME_RATE is the measured 8.6 -> SETTS_TICK = round(90000/8.6) =
    # 10465. Derive from the module so this tracks the default rather than
    # re-hard-coding a drifting number.
    tick = _rtp_forwarder.SETTS_TICK
    _rtp_forwarder._start_ffmpeg("rtsp://localhost:8554/birdfy")
    arg = _setts_arg(capture_popen.last_cmd)
    assert _rtp_forwarder.FRAME_RATE == 8.6
    assert tick == 10465  # round(90000 / 8.6)
    assert arg == f"setts=pts=N*{tick}:dts=N*{tick}:time_base=1/90000"


def test_setts_tick_is_rounded_not_truncated_for_fractional_rate(monkeypatch, capture_popen):
    # A fractional BIRDFY_FRAME_RATE must produce a *rounded* tick (the whole point
    # of recalibrating to the real ~8.6 fps): 90000/8.6 = 10465.1 -> 10465, and a
    # rate whose tick would round *up* must not truncate. Reload so FRAME_RATE and
    # SETTS_TICK re-read the env.
    monkeypatch.setenv("BIRDFY_FRAME_RATE", "8.3")  # 90000/8.3 = 10843.4 -> 10843
    mod = importlib.reload(_rtp_forwarder)
    monkeypatch.setattr(mod.subprocess, "Popen", _FakePopen)
    try:
        assert mod.FRAME_RATE == 8.3
        assert mod.SETTS_TICK == round(90000 / 8.3) == 10843
        mod._start_ffmpeg("rtsp://localhost:8554/birdfy")
        arg = _setts_arg(_FakePopen.last_cmd)
        assert arg == "setts=pts=N*10843:dts=N*10843:time_base=1/90000"
    finally:
        # Reload again with the env cleared so other tests see the default rate.
        monkeypatch.delenv("BIRDFY_FRAME_RATE", raising=False)
        importlib.reload(_rtp_forwarder)


def test_setts_tick_tracks_integer_frame_rate_override(monkeypatch, capture_popen):
    # An integer override still works (back-compat): 30 -> round(90000/30) = 3000.
    monkeypatch.setenv("BIRDFY_FRAME_RATE", "30")
    mod = importlib.reload(_rtp_forwarder)
    monkeypatch.setattr(mod.subprocess, "Popen", _FakePopen)
    try:
        mod._start_ffmpeg("rtsp://localhost:8554/birdfy")
        arg = _setts_arg(_FakePopen.last_cmd)
        assert mod.FRAME_RATE == 30
        assert arg == "setts=pts=N*3000:dts=N*3000:time_base=1/90000"  # 90000/30
    finally:
        monkeypatch.delenv("BIRDFY_FRAME_RATE", raising=False)
        importlib.reload(_rtp_forwarder)


def test_video_passthrough_and_rtsp_tcp(capture_popen):
    _rtp_forwarder._start_ffmpeg("rtsp://localhost:8554/birdfy")
    cmd = capture_popen.last_cmd
    # -c copy (no re-encode) is the whole path; output is RTSP over TCP.
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    # Two -f flags exist (-f h264 input demuxer, -f rtsp output muxer); the
    # output muxer is the one immediately preceding -rtsp_transport.
    ti = cmd.index("-rtsp_transport")
    assert cmd[ti - 2] == "-f" and cmd[ti - 1] == "rtsp"
    assert cmd[ti + 1] == "tcp"
    assert cmd[-1] == "rtsp://localhost:8554/birdfy"


def test_no_audio_adds_an_flag(capture_popen):
    _rtp_forwarder._start_ffmpeg("rtsp://localhost:8554/birdfy", audio_read_fd=None)
    assert "-an" in capture_popen.last_cmd


def test_audio_fd_maps_both_streams(capture_popen):
    # With an audio fd, both streams are mapped and audio is copied (PCMU native).
    _rtp_forwarder._start_ffmpeg("rtsp://localhost:8554/birdfy", audio_read_fd=7)
    cmd = capture_popen.last_cmd
    assert "-an" not in cmd
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert "0:v:0" in cmd and "1:a:0" in cmd
