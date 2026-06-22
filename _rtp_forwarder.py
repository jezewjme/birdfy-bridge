"""
RTP passthrough forwarder.

aiortc's libavcodec-based H264Decoder cannot decode this camera's bitstream —
a known unresolved aiortc issue (see issues #1359, #315, PR #562). The RTP we
receive is valid: FU-A fragments reassemble, in-band SPS/PPS arrive at keyframes,
and depayloading produces correct Annex B. The failure is purely in the decode
path that we don't actually need — Frigate re-encodes anyway.

This module taps the receiver between depayload and decode: when aiortc's
jitter buffer hands a complete frame back, we also push the Annex B bytes to
an ffmpeg `-c copy -f rtsp` subprocess. No decode, no encode.

How we hook in:
  RTCRtpReceiver._handle_rtp_packet calls self.__jitter_buffer.add(packet)
  which returns (pli_flag, encoded_frame). The JitterFrame.data field is the
  concatenation of every packet's depayloaded bytes — for H264 that is
  ready-to-mux Annex B. We wrap the jitter buffer's `add` method per-receiver
  so we see every assembled frame the moment it's completed.
"""
import asyncio
import logging
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from aiortc.jitterbuffer import JitterBuffer, JitterFrame

# Audio passthrough is opt-out: the camera sends a PCMU (G.711 µ-law, 8 kHz mono)
# track that aiortc depayloads to raw µ-law sample bytes. We mux that into the
# RTSP output as a second ffmpeg input with -c:a copy (no re-encode — PCMU is a
# native RTP/RTSP payload type). Set BIRDFY_AUDIO=0 to fall back to video-only.
AUDIO_ENABLED = os.getenv("BIRDFY_AUDIO", "1") not in ("0", "false", "False", "")
# Camera audio is PCMU/8000/1ch (confirmed in SDP_ANSWER: a=rtpmap:0 PCMU/8000).
# These describe the raw bytes we pipe to ffmpeg's audio input; override only if
# a future device negotiates a different G.711 variant or clock rate.
AUDIO_SAMPLE_FMT = os.getenv("BIRDFY_AUDIO_FORMAT", "mulaw")  # ffmpeg -f value
AUDIO_SAMPLE_RATE = int(os.getenv("BIRDFY_AUDIO_RATE", "8000"))
AUDIO_CHANNELS = int(os.getenv("BIRDFY_AUDIO_CHANNELS", "1"))
# ffmpeg reads the audio pipe from whatever fd os.pipe() handed us (passed via
# pass_fds and referenced as pipe:<fd>) — see _AudioPump. No fixed fd number.

logger = logging.getLogger(__name__)

# H264 Annex B start code. h264_depayload always emits 4-byte start codes.
_START_CODE = b"\x00\x00\x00\x01"

# The constant frame rate we stamp the copied stream at via `-bsf:v setts`
# (tick = round(90000/FRAME_RATE), see SETTS_TICK), and the demuxer's fps seed.
# A *float* on purpose — the camera's real rate is fractional (~8.6), and the
# A/V drift below is sensitive to fractions of an fps over a long session.
#
# IMPORTANT — this MUST match the camera's real *delivered* rate, for two reasons:
#   1. fps-cap teardown: too high and the output's media clock runs faster than
#      wall-clock, tripping Frigate's fps-cap watchdog (go2rtc "error=EOF" churn,
#      "exceeded fps limit" kills).
#   2. A/V drift: setts labels every frame exactly 1/FRAME_RATE apart, but the
#      audio rides its true 8 kHz sample clock (≈ real time). If FRAME_RATE differs
#      from the real delivered rate the video media clock runs fast/slow vs audio
#      and the two drift apart over a long session. A fixed 9 against a measured
#      8.6 ran ~4.5% fast — minutes of skew over an hour — which is why the default
#      is now the measured 8.6, not an integer 9.
# It is NOT a throttle: under -c copy ffmpeg can't resample, it only assigns
# timestamps. (We tried -use_wallclock_as_timestamps to decouple from FRAME_RATE
# entirely, but it stamps video at absolute epoch while audio starts at 0, and the
# origin gap broke Frigate's reader — see _start_ffmpeg. So FRAME_RATE matters.)
#
# The SDP advertises 15 fps but autoBitrate delivers ~8.6 fps sustained (measured:
# 30000 frames over ~3483s ≈ 8.61 fps). This is the residual-drift floor for a
# fixed CFR — autoBitrate still wobbles second-to-second, so it can't be zero
# without re-encoding the audio (aresample=async). If a future device/firmware
# delivers a different sustained rate, set BIRDFY_FRAME_RATE to match it (confirm
# against the "N frames / M bytes forwarded" log: fps ≈ Δframes / Δseconds).
#
# To reduce Frigate's *detection* CPU, set `detect: fps:` in the Frigate camera
# config instead — that's a separate downstream knob and doesn't belong here.
FRAME_RATE = float(os.getenv("BIRDFY_FRAME_RATE", "8.6") or "8.6")

# 90 kHz (RTP timebase) ticks per frame, fed to the setts bitstream filter as
# pts=dts=N*SETTS_TICK. round() (not floor) so a fractional FRAME_RATE maps to the
# nearest whole tick — e.g. 8.6 -> round(90000/8.6) = 10465. An integer division
# would silently truncate (10465.1 -> 10465 is fine, but 90000//8.6 is a TypeError
# on floats anyway). Frame N lands at N*SETTS_TICK/90000 s ≈ N/FRAME_RATE s.
SETTS_TICK = round(90000 / FRAME_RATE)

# Depth of the depayload->ffmpeg hand-off queue, in frames. The camera link is
# lossy (see the re-NACK heartbeat in _aiortc_media_patches.py): aiortc's jitter
# buffer stalls waiting on a retransmit, then releases a *burst* of held frames at
# once. The consumer hands each frame to ffmpeg via a blocking stdin write, so a
# burst transiently backs up the pipe and the queue must be deep enough to ride
# that out — otherwise it overflows and we drop frames mid-GOP (a few-second skip
# in Frigate). NOTE: sustained (not bursty) overflow means ffmpeg itself is
# stalled, not that this is too small — that was the unset-PTS muxer stall, fixed
# by the setts filter (see _start_ffmpeg); don't paper over a recurrence by just
# raising this. At ~9 fps, 512 frames ≈ 57s of slack. Override via env if needed.
QUEUE_MAXSIZE = int(os.getenv("BIRDFY_FORWARD_QUEUE", "512") or "512")

# Where ffmpeg's stderr is written. We tail this into the main log on exit so
# ffmpeg's own diagnosis ("non-existing PPS", "Invalid data", etc.) is visible
# in the bridge log rather than only in a temp file.
_FFMPEG_LOG_PATH = tempfile.gettempdir() + "/ffmpeg_birdfy_passthrough.log"


def _log_ffmpeg_tail(max_lines: int = 40) -> None:
    """Emit the tail of ffmpeg's stderr log to the bridge logger (best-effort)."""
    try:
        with open(_FFMPEG_LOG_PATH, errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    tail = lines[-max_lines:]
    if tail:
        logger.info(
            "RTP forwarder: ffmpeg stderr tail (%d of %d lines):\n%s",
            len(tail),
            len(lines),
            "".join(tail).rstrip(),
        )

# NAL unit types we care about. RFC 6184 §1.3.
NAL_SLICE = 1       # non-IDR coded slice
NAL_IDR = 5         # IDR coded slice (keyframe)
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9

# Frames at/above this size are keyframe-scale for this camera (P-frames are
# <7 KB; observed keyframes are 25-52 KB). Used to single out keyframe-sized
# frames for detailed diagnostic logging — they're rare, and they are exactly
# where the corruption shows up.
BIG_FRAME_BYTES = 12000

# How many distinct corrupt keyframe-sized frames to dump to disk for offline
# inspection before giving up (avoids filling /tmp on a stuck stream).
MAX_GARBAGE_DUMPS = 3


class _LazyHex:
    """Hex-dump the first 16 bytes of a frame, but only if the log record is
    actually emitted. Passed as a %s arg so the (per-keyframe) formatting cost
    is skipped when the level is filtered out."""

    __slots__ = ("_data",)

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __str__(self) -> str:
        return self._data[:16].hex(" ")


def _iter_nals(data: bytes):
    """Yield (nal_type, nal_bytes_with_start_code) for each NAL in Annex B data.

    aiortc's depayload emits 4-byte start codes only (\\x00\\x00\\x00\\x01); we
    don't need to handle 3-byte variants. If a malformed frame ever shows up
    without start codes, we yield nothing — caller treats as "no SPS/PPS seen".
    """
    # bytes.find() scans for the start code in C — far cheaper than a Python
    # byte-by-byte loop on a 12KB+ keyframe, which runs on the receive path.
    pos = data.find(_START_CODE)
    if pos < 0:
        return

    n = len(data)
    while pos < n:
        # We're sitting on a start code. Find the next one.
        end = data.find(_START_CODE, pos + 4)
        if end < 0:
            end = n

        nal = data[pos:end]
        if len(nal) > 4:
            nal_type = nal[4] & 0x1F
            yield nal_type, nal
        pos = end


class _ParamSetCache:
    """Holds the latest SPS/PPS NALs (with start codes) seen on the stream.

    The camera transmits SPS/PPS only as in-band STAP-A aggregates at IDR
    boundaries — they aren't in the SDP `sprop-parameter-sets`. ffmpeg's h264
    demuxer needs to see SPS+PPS before *any* slice or it errors with
    "non-existing PPS 0 referenced". We cache them on every appearance and
    prepend to each IDR we forward, so a mid-GOP join still gets a parseable
    stream once the next keyframe lands.
    """

    def __init__(self) -> None:
        self.sps: bytes | None = None
        self.pps: bytes | None = None

    @property
    def ready(self) -> bool:
        return self.sps is not None and self.pps is not None

    def observe(self, nal_type: int, nal: bytes) -> None:
        if nal_type == NAL_SPS:
            self.sps = nal
        elif nal_type == NAL_PPS:
            self.pps = nal


def _wrap_jitter_buffer(receiver, on_frame) -> None:
    """Wrap the receiver's private __jitter_buffer so we get a callback per frame.

    Calls `on_frame(jitter_frame)` whenever the buffer completes a frame.
    `on_frame` runs on the same loop/thread as _handle_rtp_packet (no locking).
    Idempotent: re-wrapping is a no-op via a marker attribute.
    """
    jb: JitterBuffer | None = getattr(receiver, "_RTCRtpReceiver__jitter_buffer", None)
    if jb is None:
        raise RuntimeError("receiver has no __jitter_buffer — aiortc internals changed")

    if getattr(jb, "_birdfy_tapped", False):
        return

    original_add = jb.add

    def add(packet):
        pli_flag, encoded_frame = original_add(packet)
        if encoded_frame is not None:
            try:
                on_frame(encoded_frame)
            except Exception:
                logger.exception("RTP forwarder on_frame callback raised")
        return pli_flag, encoded_frame

    jb.add = add  # type: ignore[method-assign]
    jb._birdfy_tapped = True  # type: ignore[attr-defined]


def _unwrap_jitter_buffer(receiver) -> None:
    jb = getattr(receiver, "_RTCRtpReceiver__jitter_buffer", None)
    if jb is None or not getattr(jb, "_birdfy_tapped", False):
        return
    # Best-effort: restore by deleting our bound wrapper so the class method shows through.
    try:
        del jb.add
    except AttributeError:
        pass
    try:
        delattr(jb, "_birdfy_tapped")
    except AttributeError:
        pass


def _start_ffmpeg(rtsp_output: str, audio_read_fd: int | None = None) -> subprocess.Popen:
    """Start ffmpeg in H264-passthrough mode writing to RTSP.

    Timestamps — the raw Annex B stream we feed in has no container timing, so we
    must synthesize it. Approaches tried and why we landed on `setts`:
      * -fflags +genpts / -fps_mode cfr cannot stamp a *copied* raw stream: they
        dup/drop against input timestamps, and raw Annex B under -c copy has none.
        Packets reached the RTSP muxer with unset PTS, ffmpeg 5.1 logged
        "Timestamps are unset in a packet", and the muxer stalled to speed≈0.2x —
        backpressuring stdin so our hand-off queue overflowed (multi-second skips).
      * -use_wallclock_as_timestamps 1 stamps video at *absolute epoch* (~1.7e9 s
        in 90 kHz units) while audio starts at ~0 (its µ-law sample count). That
        billion-second origin gap between the two tracks made downstream readers
        (Frigate's ffmpeg) find "Output file does not contain any stream" / "no
        video stream" and crash-loop. Rejected — the video PTS origin MUST be ~0 to
        match audio's sample-clock-from-0 origin.

    Current: -bsf:v setts assigns constant-rate, strictly-monotonic timestamps on
    the copied stream starting at PTS 0, in RTP's native 90 kHz timebase
    (setts=pts=N*K:dts=N*K:time_base=1/90000, K = 90000/FRAME_RATE). Frame N is at
    N/FRAME_RATE s — monotonic regardless of arrival order, no decode, and crucially
    origin-0 so audio and video line up at the start. The camera has no B-frames
    (only SLICE/IDR NALs) so PTS==DTS.

    Caveat (A/V drift): a fixed CFR labels every frame 1/FRAME_RATE apart, while
    audio rides its true 8 kHz sample clock. If FRAME_RATE != the camera's real
    delivered rate (~8.6 fps measured, autoBitrate-driven), the video media clock
    runs fast/slow vs audio and the two drift apart over a long session. The fix is
    to keep FRAME_RATE close to the real delivered rate (see the FRAME_RATE note) —
    the residual second-to-second wobble is small and bounded.

    -c copy: no re-encode (the whole point of this path).

    audio_read_fd: if given, ffmpeg reads raw PCMU (µ-law) audio from this fd as a
    second input (pipe:<fd>) and copies it into the RTSP output (-c:a copy). The
    fd is inherited by the child via Popen(pass_fds=...). ffmpeg derives the audio
    PTS from the sample clock (-f mulaw -ar 8000), starting at 0 — matching the
    setts video origin so the two inputs start aligned. Passing None keeps the
    original video-only behavior.
    """
    cmd = [
        "ffmpeg", "-y",
        # Seed the raw-H264 demuxer's fps guess. Under `-f h264` + `-c copy` this
        # does not itself stamp packets (raw Annex B carries no timing, copy bypasses
        # rate filters) — the `-bsf:v setts` below assigns the actual CFR timestamps.
        # This just keeps the demuxer's initial guess sane.
        "-r", str(FRAME_RATE),
    ]
    if audio_read_fd is not None:
        # Decouple input demux threads so a momentarily-starved audio pipe can't
        # stall the video demux (and vice versa) once running.
        cmd += ["-thread_queue_size", "512"]
    cmd += [
        "-f", "h264",
        "-i", "pipe:0",
    ]
    if audio_read_fd is not None:
        cmd += [
            # Raw G.711 µ-law has no header and we declare format/rate/channels
            # explicitly, so there is nothing to probe. The default probe waits
            # for ~0.5-5s of audio bytes before opening the input — but ffmpeg
            # won't connect the RTSP output until BOTH inputs are open, so a slow
            # audio start hung the whole publish ("no stream available"). Force
            # zero analyze/probe so audio opens instantly on the first byte.
            "-analyzeduration", "0",
            "-probesize", "32",
            "-thread_queue_size", "512",
            "-f", AUDIO_SAMPLE_FMT,
            "-ar", str(AUDIO_SAMPLE_RATE),
            "-ac", str(AUDIO_CHANNELS),
            "-i", f"pipe:{audio_read_fd}",
        ]
    cmd += [
        "-c:v", "copy",
        # Assign constant-rate, strictly-monotonic timestamps on the copied H264
        # stream, starting at PTS 0, in RTP's 90 kHz timebase. This is what keeps
        # the muxer happy AND lets Frigate see a valid video stream — the video PTS
        # origin must be ~0, matching the audio's sample-clock-from-0 origin.
        #   pts=dts=N*SETTS_TICK, SETTS_TICK = round(90000/FRAME_RATE)  → N/FRAME_RATE s
        # No B-frames (only SLICE/IDR NALs) so PTS==DTS. (We tried input wallclock
        # timestamps to share the audio's real clock and kill A/V drift, but that
        # stamps video at absolute epoch (~1.7e9 s) while audio starts at 0 — the
        # billion-second origin gap made Frigate's reader find "no video stream"
        # and crash-loop. Drift is instead bounded by matching FRAME_RATE to the
        # camera's real delivered rate; see the FRAME_RATE note above.)
        "-bsf:v",
        f"setts=pts=N*{SETTS_TICK}:dts=N*{SETTS_TICK}:time_base=1/90000",
    ]
    if audio_read_fd is not None:
        # PCMU is a native RTP/RTSP payload — copy it through, no re-encode.
        cmd += ["-c:a", "copy", "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-an"]
    cmd += [
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        rtsp_output,
    ]
    logger.info("RTP forwarder ffmpeg: %s", " ".join(cmd))
    # The child inherits its own copy of the stderr handle; close the parent's
    # right after Popen so each ffmpeg (re)start doesn't leak a file handle.
    with open(_FFMPEG_LOG_PATH, "w") as stderr_log:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr_log,
            pass_fds=() if audio_read_fd is None else (audio_read_fd,),
        )


class _AudioPump:
    """Owns the OS pipe that carries µ-law audio to one ffmpeg instance and the
    background task that drains the audio frame queue into it.

    One pump is created per ffmpeg (re)start and closed when that ffmpeg is reaped,
    so the write end never outlives its reader. The read end is handed to ffmpeg
    via pass_fds and closed in the parent right after Popen (the child keeps it).
    """

    def __init__(self, queue: asyncio.Queue[bytes]) -> None:
        self._queue = queue
        self.read_fd, self._write_fd = os.pipe()
        self._writer: asyncio.Task | None = None
        self._closed = False

    def start_writer(self) -> None:
        self._writer = asyncio.ensure_future(self._pump())

    def close_read_fd_in_parent(self) -> None:
        """After Popen the child owns the read end; the parent must drop its copy
        or ffmpeg never sees EOF when we close the write end."""
        try:
            os.close(self.read_fd)
        except OSError:
            pass

    async def _pump(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            # Prime the pipe with a few ms of µ-law silence (0xFF == silence in
            # µ-law) so ffmpeg's audio input opens on the very first read instead
            # of blocking until the camera sends real audio. ffmpeg won't connect
            # the RTSP output until BOTH inputs are open, so an unprimed audio
            # pipe could hang the whole publish if the camera gates audio. ~50ms
            # @ 8 kHz mono = 400 bytes — inaudible, just unblocks startup.
            try:
                await loop.run_in_executor(
                    None, _write_all, self._write_fd, b"\xff" * (AUDIO_SAMPLE_RATE // 20)
                )
            except (BrokenPipeError, OSError):
                return
            while True:
                data = await self._queue.get()
                if data is None:  # sentinel — shutdown
                    return
                try:
                    # os.write can short-write on a full pipe; loop. Run in the
                    # executor so a momentarily-full pipe doesn't block the loop.
                    await loop.run_in_executor(None, _write_all, self._write_fd, data)
                except (BrokenPipeError, OSError):
                    return
        except asyncio.CancelledError:
            return

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer is not None and not self._writer.done():
            self._writer.cancel()
        try:
            os.close(self._write_fd)
        except OSError:
            pass


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of `data` to `fd`, retrying short writes."""
    mv = memoryview(data)
    while mv:
        n = os.write(fd, mv)
        mv = mv[n:]


def _reap_ffmpeg(proc: subprocess.Popen | None) -> None:
    """Best-effort cleanup of a dead/dying ffmpeg so we don't leak the process or
    its stdin pipe when restarting. Safe to call on an already-exited process."""
    if proc is None:
        return
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except Exception:
        pass
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def forward_video(
    receiver,
    rtsp_output: str,
    frame_timeout: float = 90.0,
    audio_receiver=None,
) -> None:
    """Pump depayloaded H264 frames from `receiver` to ffmpeg → RTSP.

    Returns when no frame has arrived for `frame_timeout` seconds, when ffmpeg
    dies, or when the caller cancels.

    audio_receiver: if given (and BIRDFY_AUDIO not disabled), the camera's PCMU
    audio track is tapped the same way as video and muxed into the RTSP output
    via a second ffmpeg input (-c:a copy). The video path is unchanged when no
    audio receiver is supplied or audio is disabled.
    """
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    # Rate-limit the "queue full" warning: a burst overflow would otherwise emit
    # hundreds of identical lines per second. We coalesce into one WARNING every
    # ~2s carrying the running drop count so the signal survives without the spam.
    drop_state = {"count": 0, "last_log": 0.0}

    def _on_frame(jf: JitterFrame) -> None:
        # Called synchronously from _handle_rtp_packet on the receive loop.
        # put_nowait keeps us off the slow path; if backed up we drop.
        if not jf.data:
            return
        try:
            queue.put_nowait(jf.data)
        except asyncio.QueueFull:
            drop_state["count"] += 1
            now = time.monotonic()
            if now - drop_state["last_log"] >= 2.0:
                logger.warning(
                    "RTP forwarder queue full — dropping frames (%d dropped total, "
                    "depth=%d). Link is bursty; raise BIRDFY_FORWARD_QUEUE if frequent.",
                    drop_state["count"],
                    QUEUE_MAXSIZE,
                )
                drop_state["last_log"] = now

    _wrap_jitter_buffer(receiver, _on_frame)

    # ── Audio tap (optional) ──────────────────────────────────────────────────
    # The audio queue is filled continuously from the moment the track is tapped;
    # an _AudioPump (created per ffmpeg start) drains it into ffmpeg's audio pipe.
    # While no ffmpeg is running the queue just buffers/drops — bounded so a long
    # pre-IDR wait can't grow it without limit.
    # pass_fds (used to hand the audio pipe to ffmpeg) is POSIX-only. The bridge
    # ships in a Linux container; on a non-POSIX dev host degrade to video-only
    # rather than crash on Popen(pass_fds=...).
    audio_enabled = AUDIO_ENABLED and audio_receiver is not None and os.name == "posix"
    if AUDIO_ENABLED and audio_receiver is not None and os.name != "posix":
        logger.warning(
            "RTP forwarder: audio passthrough needs POSIX (pass_fds) — video-only on %s",
            os.name,
        )
    audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
    audio_frames_seen = 0
    audio_pump: _AudioPump | None = None

    if audio_enabled:
        def _on_audio(jf: JitterFrame) -> None:
            nonlocal audio_frames_seen
            if not jf.data:
                return
            audio_frames_seen += 1
            try:
                audio_queue.put_nowait(jf.data)
            except asyncio.QueueFull:
                # Drop oldest so we favor fresh audio (avoids unbounded a/v skew
                # if ffmpeg isn't draining yet).
                try:
                    audio_queue.get_nowait()
                    audio_queue.put_nowait(jf.data)
                except asyncio.QueueEmpty:
                    pass

        try:
            _wrap_jitter_buffer(audio_receiver, _on_audio)
            logger.info("RTP forwarder: audio passthrough enabled (PCMU -> -c:a copy)")
        except Exception as e:
            audio_enabled = False
            logger.warning("RTP forwarder: could not tap audio receiver (%s) — video-only", e)

    def _start_av_ffmpeg() -> tuple[subprocess.Popen, _AudioPump | None]:
        """Start ffmpeg, wiring an audio pump iff audio is enabled."""
        if not audio_enabled:
            return _start_ffmpeg(rtsp_output), None
        pump = _AudioPump(audio_queue)
        p = _start_ffmpeg(rtsp_output, audio_read_fd=pump.read_fd)
        pump.close_read_fd_in_parent()
        pump.start_writer()
        return p, pump

    loop = asyncio.get_event_loop()
    # Dedicated single thread for ffmpeg stdin writes. proc.stdin.write blocks
    # when ffmpeg's input pipe is full (it paces output at CFR, so a burst of
    # held frames backs the pipe up). On the *shared* default executor that
    # blocking call competes with the audio pump and every other run_in_executor
    # caller — a stalled video write could starve audio (and vice versa), and a
    # busy pool delays the next write, deepening the queue backlog. A private
    # one-thread pool guarantees video writes are serialized only against
    # themselves and start the instant the previous one returns.
    write_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ffmpeg-stdin")
    proc: subprocess.Popen | None = None
    params = _ParamSetCache()
    pre_proc_frames_dropped = 0
    frames_in = 0
    bytes_in = 0
    # Diagnostics for keyframe-recovery debugging (see _aiortc_media_patches.py).
    total_frames_seen = 0
    garbage_frames_seen = 0   # frames with no Annex B start code (lost head)
    idr_frames_seen = 0
    garbage_dumps = 0
    # Resilience counters: a single bad frame or transient ffmpeg death must not
    # kill the stream permanently (it used to — ffmpeg died on a headerless frame
    # and was never restarted, leaving MediaMTX with "no stream is available").
    garbage_frames_skipped = 0
    ffmpeg_restarts = 0
    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=frame_timeout)
            except asyncio.TimeoutError:
                logger.warning("RTP forwarder: no frame for %ss — exiting", frame_timeout)
                return

            # Split frame into NALs, cache any SPS/PPS we see, and check for IDR.
            nal_types: list[int] = []
            for nal_type, nal in _iter_nals(data):
                nal_types.append(nal_type)
                params.observe(nal_type, nal)

            has_idr = NAL_IDR in nal_types
            has_start_code = data[:4] == _START_CODE
            # A "garbage" frame is one with no Annex B start code anywhere — the
            # signature of a keyframe whose head fragment was lost/evicted (see
            # _aiortc_media_patches.py). Track these explicitly so we can tell
            # whether the NACK/jitter fix actually recovered the keyframe head.
            is_garbage = not nal_types

            total_frames_seen += 1
            if is_garbage:
                garbage_frames_seen += 1
            if has_idr:
                idr_frames_seen += 1

            # Debug: log the first few frames' shape so we can see what aiortc
            # actually hands us (start-code layout, NAL types observed).
            if pre_proc_frames_dropped < 5 and proc is None:
                logger.info(
                    "RTP forwarder: frame %d bytes head=%s start_code=%s nal_types=%s",
                    len(data),
                    _LazyHex(data),
                    has_start_code,
                    nal_types,
                )

            # Big frames are keyframe-sized. A clean keyframe in steady state is
            # routine, so log it at DEBUG; only escalate to a WARNING when a big
            # frame is garbage (no start code = lost/evicted keyframe head), which
            # is the failure we care about. Either way include the running tally
            # so a debug session can see whether keyframes stay clean.
            if len(data) >= BIG_FRAME_BYTES:
                _level = logging.WARNING if is_garbage else logging.DEBUG
                logger.log(
                    _level,
                    "RTP forwarder: BIG frame %d bytes head=%s start_code=%s "
                    "nal_types=%s garbage=%s (tally: frames=%d garbage=%d idr=%d)",
                    len(data),
                    _LazyHex(data),
                    has_start_code,
                    nal_types,
                    is_garbage,
                    total_frames_seen,
                    garbage_frames_seen,
                    idr_frames_seen,
                )
                # Dump the first few corrupt big frames to disk for offline
                # inspection (hex/structure) if the fix didn't take.
                if is_garbage and garbage_dumps < MAX_GARBAGE_DUMPS:
                    garbage_dumps += 1
                    try:
                        dump_path = (
                            f"{tempfile.gettempdir()}/birdfy_garbage_frame_{garbage_dumps}.bin"
                        )
                        with open(dump_path, "wb") as f:
                            f.write(data)
                        logger.info(
                            "RTP forwarder: dumped corrupt frame #%d (%d bytes) to %s",
                            garbage_dumps,
                            len(data),
                            dump_path,
                        )
                    except Exception as e:
                        logger.debug("garbage frame dump failed: %s", e)

            # A headerless frame (no Annex B start code / no parseable NALs) is the
            # signature of a keyframe whose head fragment was lost despite the NACK
            # widening. It is unusable to the -c copy muxer: feeding it makes ffmpeg
            # log "Invalid data found" and exit, which previously killed the stream
            # permanently. Skipping it costs exactly one frame; the next keyframe
            # re-syncs the decoder. Never write garbage to ffmpeg.
            if is_garbage:
                garbage_frames_skipped += 1
                continue

            if proc is None:
                # Wait for a keyframe AND a known SPS+PPS before starting ffmpeg,
                # otherwise the h264 demuxer errors with "non-existing PPS 0
                # referenced" on the slices it sees first.
                if not (params.ready and has_idr):
                    pre_proc_frames_dropped += 1
                    if pre_proc_frames_dropped in (1, 10, 50) or pre_proc_frames_dropped % 100 == 0:
                        logger.info(
                            "RTP forwarder: waiting for SPS+PPS+IDR (dropped %d frames, "
                            "sps=%s pps=%s idr=%s | seen=%d garbage=%d idr_total=%d)",
                            pre_proc_frames_dropped,
                            params.sps is not None,
                            params.pps is not None,
                            has_idr,
                            total_frames_seen,
                            garbage_frames_seen,
                            idr_frames_seen,
                        )
                    continue
                proc, audio_pump = _start_av_ffmpeg()
                logger.info(
                    "RTP forwarder: ffmpeg started after %d pre-IDR drops, first IDR frame %d bytes (audio=%s)",
                    pre_proc_frames_dropped,
                    len(data),
                    audio_enabled,
                )

            if proc.poll() is not None:
                logger.warning(
                    "RTP forwarder: ffmpeg exited with code %s — will restart on next keyframe",
                    proc.returncode,
                )
                _log_ffmpeg_tail()
                _reap_ffmpeg(proc)
                if audio_pump is not None:
                    audio_pump.close()
                    audio_pump = None
                proc = None
                ffmpeg_restarts += 1
                # Re-arm the keyframe gate so the fresh ffmpeg starts on a clean
                # SPS+PPS+IDR boundary rather than mid-GOP.
                pre_proc_frames_dropped = 0
                continue

            # Prepend cached SPS+PPS to every IDR so a downstream decoder that
            # tunes in mid-stream (or restarts after errors) can re-sync.
            if has_idr and NAL_SPS not in nal_types and NAL_PPS not in nal_types:
                out = params.sps + params.pps + data  # type: ignore[operator]
            else:
                out = data

            try:
                # Blocking write — offload to our dedicated single-thread pool so
                # a momentarily-stalled ffmpeg (full stdin pipe) can't freeze the
                # event loop. If the loop blocks here, the RTP receive loop, audio
                # pump, and WS heartbeat all stall, the RTSP publish goes silent,
                # and MediaMTX times out the publisher (the ~7-min reconnect/restart
                # cascade seen after the aiortc/av bumps changed frame pacing).
                # write_pool (not the shared default executor) keeps these writes
                # off the pool the audio pump and other tasks share.
                await loop.run_in_executor(write_pool, proc.stdin.write, out)  # type: ignore[union-attr]
            except BrokenPipeError:
                logger.warning("RTP forwarder: ffmpeg pipe broken — will restart on next keyframe")
                _log_ffmpeg_tail()
                _reap_ffmpeg(proc)
                if audio_pump is not None:
                    audio_pump.close()
                    audio_pump = None
                proc = None
                ffmpeg_restarts += 1
                pre_proc_frames_dropped = 0
                continue

            frames_in += 1
            bytes_in += len(out)
            if frames_in in (1, 10, 100) or frames_in % 500 == 0:
                logger.info(
                    "RTP forwarder: %d frames / %d bytes forwarded",
                    frames_in,
                    bytes_in,
                )
    finally:
        logger.info(
            "RTP forwarder summary: frames_seen=%d garbage(no-start-code)=%d "
            "garbage_skipped=%d idr_total=%d frames_forwarded=%d bytes_forwarded=%d "
            "ffmpeg_restarts=%d queue_drops=%d audio_frames=%d audio_enabled=%s "
            "ffmpeg_started=%s",
            total_frames_seen,
            garbage_frames_seen,
            garbage_frames_skipped,
            idr_frames_seen,
            frames_in,
            bytes_in,
            ffmpeg_restarts,
            drop_state["count"],
            audio_frames_seen,
            audio_enabled,
            proc is not None,
        )
        if audio_pump is not None:
            audio_pump.close()
        _unwrap_jitter_buffer(receiver)
        if audio_enabled:
            _unwrap_jitter_buffer(audio_receiver)
        _reap_ffmpeg(proc)
        # Tear down the dedicated writer thread. ffmpeg's stdin is closed above,
        # so any in-flight write has unblocked; don't wait on a wedged write.
        write_pool.shutdown(wait=False)
