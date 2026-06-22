"""Drive the forward_video() main loop with a fake receiver + fake ffmpeg.

This is the one big coroutine in _rtp_forwarder. Rather than fake the whole
WebRTC/aiortc stack, we use the real jitter-buffer tap (_wrap_jitter_buffer) on a
fake receiver and push crafted Annex B frames through it, with _start_ffmpeg
stubbed to a fake process. That exercises the real frame-split / keyframe-gate /
ffmpeg-start / write / garbage-skip / timeout-exit logic without a live camera.
"""
import asyncio

import pytest

pytest.importorskip("aiortc", reason="_rtp_forwarder imports aiortc at module load")

import _rtp_forwarder as rf  # noqa: E402

_START = rf._START_CODE


def _nal(nal_type: int, payload: bytes = b"\x00\x00") -> bytes:
    return _START + bytes([nal_type & 0x1F]) + payload


# A receiver whose jitter buffer we can pump frames through manually.
class _FakeJB:
    def __init__(self):
        self._birdfy_tapped = False

    def add(self, packet):
        # The real wrapper replaces this; the base returns nothing useful.
        return (False, None)


class _FakeReceiver:
    def __init__(self):
        self._RTCRtpReceiver__jitter_buffer = _FakeJB()


class _Frame:
    """Stands in for aiortc's JitterFrame (only .data is read)."""

    def __init__(self, data):
        self.data = data


class _FakeProc:
    def __init__(self):
        self.stdin = _Stdin()
        self._alive = True
        self.returncode = None

    def poll(self):
        if self._alive:
            return None
        self.returncode = 1
        return self.returncode

    def terminate(self):
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


class _Stdin:
    def __init__(self):
        self.chunks = []
        self.closed = False

    def write(self, data):
        self.chunks.append(bytes(data))

    def close(self):
        self.closed = True


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    procs = []

    def _start(rtsp_output, audio_read_fd=None):
        p = _FakeProc()
        procs.append(p)
        return p

    monkeypatch.setattr(rf, "_start_ffmpeg", _start)
    return procs


@pytest.mark.asyncio
async def test_forward_video_starts_ffmpeg_on_keyframe_and_writes(fake_ffmpeg, monkeypatch):
    receiver = _FakeReceiver()

    # We'll capture the tap callback the forwarder installs, then push frames.
    pushed = {}

    real_wrap = rf._wrap_jitter_buffer

    def capture_wrap(recv, on_frame):
        pushed["on_frame"] = on_frame
        real_wrap(recv, on_frame)

    monkeypatch.setattr(rf, "_wrap_jitter_buffer", capture_wrap)

    # Run forward_video with a short timeout so it exits on its own after we feed
    # frames. Feed: SPS, PPS, IDR (starts ffmpeg + writes), then a P-slice (writes).
    async def feeder():
        # Wait for the tap to be installed.
        for _ in range(100):
            await asyncio.sleep(0)
            if "on_frame" in pushed:
                break
        on_frame = pushed["on_frame"]
        # One combined access unit with SPS+PPS+IDR starts ffmpeg immediately.
        on_frame(_Frame(_nal(rf.NAL_SPS) + _nal(rf.NAL_PPS) + _nal(rf.NAL_IDR, b"\xaa" * 20)))
        on_frame(_Frame(_nal(rf.NAL_SLICE, b"\xbb" * 10)))  # P-frame -> write
        # Give the loop time to consume both, then it will hit the timeout.
        await asyncio.sleep(0.05)

    await asyncio.wait_for(
        asyncio.gather(
            rf.forward_video(receiver, "rtsp://localhost/birdfy", frame_timeout=0.1),
            feeder(),
        ),
        timeout=3.0,
    )

    assert fake_ffmpeg, "ffmpeg was never started"
    # The IDR access unit and the P-frame were both written to ffmpeg's stdin.
    assert len(fake_ffmpeg[0].stdin.chunks) >= 2


@pytest.mark.asyncio
async def test_forward_video_skips_garbage_and_times_out(fake_ffmpeg, monkeypatch):
    receiver = _FakeReceiver()
    pushed = {}
    real_wrap = rf._wrap_jitter_buffer

    def capture_wrap(recv, on_frame):
        pushed["on_frame"] = on_frame
        real_wrap(recv, on_frame)

    monkeypatch.setattr(rf, "_wrap_jitter_buffer", capture_wrap)

    async def feeder():
        for _ in range(100):
            await asyncio.sleep(0)
            if "on_frame" in pushed:
                break
        # A frame with NO Annex B start code = garbage (lost keyframe head). It
        # must be skipped, never written, and ffmpeg must never start.
        pushed["on_frame"](_Frame(b"\x11\x22\x33\x44" * 10))
        await asyncio.sleep(0.02)

    await asyncio.wait_for(
        asyncio.gather(
            rf.forward_video(receiver, "rtsp://localhost/birdfy", frame_timeout=0.1),
            feeder(),
        ),
        timeout=3.0,
    )
    # Garbage never starts ffmpeg.
    assert fake_ffmpeg == []


@pytest.mark.asyncio
async def test_forward_video_waits_for_sps_pps_before_starting(fake_ffmpeg, monkeypatch):
    receiver = _FakeReceiver()
    pushed = {}
    real_wrap = rf._wrap_jitter_buffer

    def capture_wrap(recv, on_frame):
        pushed["on_frame"] = on_frame
        real_wrap(recv, on_frame)

    monkeypatch.setattr(rf, "_wrap_jitter_buffer", capture_wrap)

    async def feeder():
        for _ in range(100):
            await asyncio.sleep(0)
            if "on_frame" in pushed:
                break
        # An IDR with no preceding SPS/PPS must NOT start ffmpeg (the demuxer
        # would error "non-existing PPS"). It's dropped while the gate waits.
        pushed["on_frame"](_Frame(_nal(rf.NAL_IDR, b"\xaa" * 20)))
        await asyncio.sleep(0.02)

    await asyncio.wait_for(
        asyncio.gather(
            rf.forward_video(receiver, "rtsp://localhost/birdfy", frame_timeout=0.1),
            feeder(),
        ),
        timeout=3.0,
    )
    assert fake_ffmpeg == []  # gate held: no SPS+PPS yet


@pytest.mark.asyncio
async def test_forward_video_restarts_after_ffmpeg_dies(fake_ffmpeg, monkeypatch):
    receiver = _FakeReceiver()
    pushed = {}
    real_wrap = rf._wrap_jitter_buffer

    def capture_wrap(recv, on_frame):
        pushed["on_frame"] = on_frame
        real_wrap(recv, on_frame)

    monkeypatch.setattr(rf, "_wrap_jitter_buffer", capture_wrap)

    keyframe = _nal(rf.NAL_SPS) + _nal(rf.NAL_PPS) + _nal(rf.NAL_IDR, b"\xaa" * 20)

    async def feeder():
        for _ in range(100):
            await asyncio.sleep(0)
            if "on_frame" in pushed:
                break
        on_frame = pushed["on_frame"]
        on_frame(_Frame(keyframe))           # starts ffmpeg #1
        await asyncio.sleep(0.02)
        # Kill ffmpeg #1, then send another keyframe -> loop notices poll()!=None,
        # reaps it, and starts ffmpeg #2 on the next SPS+PPS+IDR.
        if fake_ffmpeg:
            fake_ffmpeg[0]._alive = False
        on_frame(_Frame(keyframe))           # triggers restart path
        on_frame(_Frame(keyframe))           # starts ffmpeg #2
        await asyncio.sleep(0.05)

    await asyncio.wait_for(
        asyncio.gather(
            rf.forward_video(receiver, "rtsp://localhost/birdfy", frame_timeout=0.1),
            feeder(),
        ),
        timeout=3.0,
    )
    # A second ffmpeg was spawned after the first died.
    assert len(fake_ffmpeg) >= 2


@pytest.mark.asyncio
async def test_forward_video_coalesces_dropped_frames(monkeypatch):
    # With a tiny queue, flooding _on_frame faster than the consumer drains must
    # increment the drop counter and not raise (the coalescing warning path).
    monkeypatch.setattr(rf, "QUEUE_MAXSIZE", 2)
    receiver = _FakeReceiver()
    pushed = {}
    real_wrap = rf._wrap_jitter_buffer

    def capture_wrap(recv, on_frame):
        pushed["on_frame"] = on_frame
        real_wrap(recv, on_frame)

    monkeypatch.setattr(rf, "_wrap_jitter_buffer", capture_wrap)

    async def feeder():
        for _ in range(100):
            await asyncio.sleep(0)
            if "on_frame" in pushed:
                break
        on_frame = pushed["on_frame"]
        # Synchronously push way more than the queue holds before yielding, so
        # the consumer never gets a chance to drain — forces QueueFull drops.
        for _ in range(20):
            on_frame(_Frame(_nal(rf.NAL_SLICE, b"\xbb" * 4)))
        await asyncio.sleep(0.02)

    # No ffmpeg fake needed — these are all garbage-free P-frames that never form
    # a start gate, so ffmpeg won't start; we only care that drops don't raise.
    await asyncio.wait_for(
        asyncio.gather(
            rf.forward_video(receiver, "rtsp://localhost/birdfy", frame_timeout=0.1),
            feeder(),
        ),
        timeout=3.0,
    )  # completes without raising = drop coalescing handled QueueFull


@pytest.mark.asyncio
async def test_forward_video_audio_enabled_taps_audio_and_starts_with_pump(
    fake_ffmpeg, monkeypatch
):
    # Force the POSIX audio gate on (the bridge runs in a Linux container) and
    # feed an audio receiver. The audio tap must be installed and ffmpeg started
    # with an audio fd / pump.
    monkeypatch.setattr(rf, "AUDIO_ENABLED", True)
    monkeypatch.setattr(rf.os, "name", "posix")

    audio_fds = []

    def _start(rtsp_output, audio_read_fd=None):
        audio_fds.append(audio_read_fd)
        return _FakeProc()

    monkeypatch.setattr(rf, "_start_ffmpeg", _start)

    video_recv = _FakeReceiver()
    audio_recv = _FakeReceiver()

    taps = {}
    real_wrap = rf._wrap_jitter_buffer

    def capture_wrap(recv, on_frame):
        taps[id(recv)] = on_frame
        real_wrap(recv, on_frame)

    monkeypatch.setattr(rf, "_wrap_jitter_buffer", capture_wrap)

    keyframe = _nal(rf.NAL_SPS) + _nal(rf.NAL_PPS) + _nal(rf.NAL_IDR, b"\xaa" * 20)

    async def feeder():
        for _ in range(200):
            await asyncio.sleep(0)
            if id(video_recv) in taps and id(audio_recv) in taps:
                break
        # Push an audio frame (exercises _on_audio) and a video keyframe.
        taps[id(audio_recv)](_Frame(b"\xff" * 160))
        taps[id(video_recv)](_Frame(keyframe))
        await asyncio.sleep(0.05)

    await asyncio.wait_for(
        asyncio.gather(
            rf.forward_video(
                video_recv, "rtsp://localhost/birdfy",
                frame_timeout=0.1, audio_receiver=audio_recv,
            ),
            feeder(),
        ),
        timeout=3.0,
    )
    # ffmpeg was started with a real audio fd (audio pump wired in).
    assert audio_fds and audio_fds[0] is not None


def test_audio_pump_primes_and_shuts_down():
    # _AudioPump uses os.pipe()/os.write, which work on any platform. Construct
    # one, prime+drain a frame, then close — covering the pump lifecycle.
    import asyncio as _asyncio

    async def _run():
        q: _asyncio.Queue = _asyncio.Queue()
        pump = rf._AudioPump(q)
        pump.start_writer()
        await q.put(b"\xff" * 80)        # one audio frame to drain
        await _asyncio.sleep(0.02)
        pump.close()
        # Idempotent close.
        pump.close()

    _asyncio.run(_run())
