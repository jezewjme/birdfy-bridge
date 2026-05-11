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
import subprocess
import tempfile
from typing import Optional

from aiortc.jitterbuffer import JitterBuffer, JitterFrame

logger = logging.getLogger(__name__)

# H264 Annex B start code. h264_depayload always emits 4-byte start codes.
_START_CODE = b"\x00\x00\x00\x01"

# NAL unit types we care about. RFC 6184 §1.3.
NAL_SLICE = 1       # non-IDR coded slice
NAL_IDR = 5         # IDR coded slice (keyframe)
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9


def _iter_nals(data: bytes):
    """Yield (nal_type, nal_bytes_with_start_code) for each NAL in Annex B data.

    aiortc's depayload emits 4-byte start codes only (\\x00\\x00\\x00\\x01); we
    don't need to handle 3-byte variants. If a malformed frame ever shows up
    without start codes, we yield nothing — caller treats as "no SPS/PPS seen".
    """
    pos = 0
    n = len(data)
    # Find the first start code
    while pos < n - 3:
        if data[pos:pos + 4] == _START_CODE:
            break
        pos += 1
    else:
        return

    while pos < n:
        # We're sitting on a start code. Find the next one.
        end = pos + 4
        while end < n - 3:
            if data[end:end + 4] == _START_CODE:
                break
            end += 1
        else:
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
        self.sps: Optional[bytes] = None
        self.pps: Optional[bytes] = None

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
    jb: Optional[JitterBuffer] = getattr(receiver, "_RTCRtpReceiver__jitter_buffer", None)
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


def _start_ffmpeg(rtsp_output: str) -> subprocess.Popen:
    """Start ffmpeg in H264-passthrough mode writing to RTSP.

    -fflags +genpts + -use_wallclock_as_timestamps 1: the raw Annex B stream we
      feed in has no container timing, so let ffmpeg stamp at arrival wall time.
    -c copy: no re-encode (the whole point of this path).
    """
    cmd = [
        "ffmpeg", "-y",
        "-fflags", "+genpts",
        "-use_wallclock_as_timestamps", "1",
        "-f", "h264",
        "-i", "pipe:0",
        "-c:v", "copy",
        "-an",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        rtsp_output,
    ]
    logger.info("RTP forwarder ffmpeg: %s", " ".join(cmd))
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=open(tempfile.gettempdir() + "/ffmpeg_birdfy_passthrough.log", "w"),
    )


async def forward_video(receiver, rtsp_output: str, frame_timeout: float = 90.0) -> None:
    """Pump depayloaded H264 frames from `receiver` to ffmpeg → RTSP.

    Returns when no frame has arrived for `frame_timeout` seconds, when ffmpeg
    dies, or when the caller cancels.
    """
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)

    def _on_frame(jf: JitterFrame) -> None:
        # Called synchronously from _handle_rtp_packet on the receive loop.
        # put_nowait keeps us off the slow path; if backed up we drop.
        if not jf.data:
            return
        try:
            queue.put_nowait(jf.data)
        except asyncio.QueueFull:
            logger.warning("RTP forwarder queue full — dropping frame")

    _wrap_jitter_buffer(receiver, _on_frame)

    proc: Optional[subprocess.Popen] = None
    params = _ParamSetCache()
    pre_proc_frames_dropped = 0
    frames_in = 0
    bytes_in = 0
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

            # Debug: log the first few frames' shape so we can see what aiortc
            # actually hands us (start-code layout, NAL types observed).
            if pre_proc_frames_dropped < 5 and proc is None:
                head = data[:16].hex(" ")
                logger.info(
                    "RTP forwarder: frame %d bytes head=%s nal_types=%s",
                    len(data),
                    head,
                    nal_types,
                )

            if proc is None:
                # Wait for a keyframe AND a known SPS+PPS before starting ffmpeg,
                # otherwise the h264 demuxer errors with "non-existing PPS 0
                # referenced" on the slices it sees first.
                if not (params.ready and has_idr):
                    pre_proc_frames_dropped += 1
                    if pre_proc_frames_dropped in (1, 10, 50) or pre_proc_frames_dropped % 100 == 0:
                        logger.info(
                            "RTP forwarder: waiting for SPS+PPS+IDR (dropped %d frames, sps=%s pps=%s idr=%s)",
                            pre_proc_frames_dropped,
                            params.sps is not None,
                            params.pps is not None,
                            has_idr,
                        )
                    continue
                proc = _start_ffmpeg(rtsp_output)
                logger.info(
                    "RTP forwarder: ffmpeg started after %d pre-IDR drops, first IDR frame %d bytes",
                    pre_proc_frames_dropped,
                    len(data),
                )

            if proc.poll() is not None:
                logger.warning("RTP forwarder: ffmpeg exited with code %s", proc.returncode)
                return

            # Prepend cached SPS+PPS to every IDR so a downstream decoder that
            # tunes in mid-stream (or restarts after errors) can re-sync.
            if has_idr and NAL_SPS not in nal_types and NAL_PPS not in nal_types:
                out = params.sps + params.pps + data  # type: ignore[operator]
            else:
                out = data

            try:
                proc.stdin.write(out)  # type: ignore[union-attr]
            except BrokenPipeError:
                logger.warning("RTP forwarder: ffmpeg pipe broken")
                return

            frames_in += 1
            bytes_in += len(out)
            if frames_in in (1, 10, 100) or frames_in % 500 == 0:
                logger.info(
                    "RTP forwarder: %d frames / %d bytes forwarded",
                    frames_in,
                    bytes_in,
                )
    finally:
        _unwrap_jitter_buffer(receiver)
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
