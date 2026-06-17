"""Tests for the ffmpeg command-building in _rtp_forwarder._start_ffmpeg().

The current timestamp approach: drive BOTH video and audio off the same real
wall-clock so A/V can't drift. Video uses -use_wallclock_as_timestamps on the
input (forward_video paces stdin writes to each frame's true completion time, so
ffmpeg's read-time reproduces the real delivery cadence); audio keeps its 8 kHz
µ-law sample clock. The earlier fixed-CFR `setts` filter was removed because a
constant FRAME_RATE could not track the camera's wobbling delivered rate and so
drifted against audio's true sample clock.

We capture the argv handed to subprocess.Popen instead of spawning ffmpeg.
"""
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


def test_uses_input_wallclock_not_setts_genpts_or_fps_mode(capture_popen):
    _rtp_forwarder._start_ffmpeg("rtsp://localhost:8554/birdfy")
    cmd = capture_popen.last_cmd
    # Timing now comes from input wallclock timestamps; the old per-frame knobs
    # are all gone (setts CFR drifted vs audio; genpts/fps_mode can't stamp copy).
    assert cmd[cmd.index("-use_wallclock_as_timestamps") + 1] == "1"
    assert "-bsf:v" not in cmd
    assert "+genpts" not in cmd
    assert "-fps_mode" not in cmd


def test_wallclock_applies_to_video_input(capture_popen):
    # -use_wallclock_as_timestamps must precede the video input (-i pipe:0) so it
    # binds to that demuxer, not to a later one.
    _rtp_forwarder._start_ffmpeg("rtsp://localhost:8554/birdfy")
    cmd = capture_popen.last_cmd
    wc = cmd.index("-use_wallclock_as_timestamps")
    video_in = cmd.index("pipe:0")
    assert wc < video_in


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
