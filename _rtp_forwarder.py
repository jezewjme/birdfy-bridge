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
    frames_in = 0
    bytes_in = 0
    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=frame_timeout)
            except asyncio.TimeoutError:
                logger.warning("RTP forwarder: no frame for %ss — exiting", frame_timeout)
                return

            if proc is None:
                proc = _start_ffmpeg(rtsp_output)
                logger.info("RTP forwarder: ffmpeg started, first frame %d bytes", len(data))

            if proc.poll() is not None:
                logger.warning("RTP forwarder: ffmpeg exited with code %s", proc.returncode)
                return

            try:
                proc.stdin.write(data)  # type: ignore[union-attr]
            except BrokenPipeError:
                logger.warning("RTP forwarder: ffmpeg pipe broken")
                return

            frames_in += 1
            bytes_in += len(data)
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
