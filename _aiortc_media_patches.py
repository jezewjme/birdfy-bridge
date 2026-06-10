"""
Runtime monkey-patches for aiortc's media (RTP) receive path.

Problem these fix (confirmed from captured bridge debug logs + reading the
aiortc source):

  The bridge negotiates H264 fine and RTP flows, but every *keyframe* arrives at
  our forwarder as headerless garbage (no `00 00 00 01` start code anywhere,
  `nal_types=[]`), while small P-frames come through clean. aiortc's own decoder
  also fails on the same frames ("Invalid data found"). So the corruption is in
  aiortc's depayload/jitter path, upstream of our code.

  Mechanism:
    * The camera's keyframe is large (~25-52 KB) -> ~50-110 RTP packets of 500 B,
      sent as a long run of FU-A fragments with one timestamp.
    * aiortc's video JitterBuffer is only capacity=128 (jitterbuffer.py). A
      single keyframe nearly fills it, so any reordering or an as-yet-unrecovered
      lost packet trips `delta >= capacity` -> `smart_remove()` evicts the HEAD of
      the keyframe, including the FU-A *start* fragment that carries the NAL
      header + start code.
    * JitterBuffer._remove_frame then concatenates the surviving continuation
      fragments with no leading start code -> headerless garbage.
    * aiortc DOES send receive-side NACK, but only ONCE at the moment a gap is
      first observed (rtcrtpreceiver.py:515). If that NACK (or the camera's
      retransmission) is lost, the packet is never recovered and the keyframe
      head is evicted before it can arrive. NackGenerator also only tracks gaps
      within RTP_HISTORY_SIZE=128 of max_seq -- about one keyframe's worth.

These patches:

  1. WIDEN the video jitter buffer 128 -> JITTER_CAPACITY (default 2048). A 52 KB
     keyframe is ~105 packets; 2048 leaves ~19 keyframes of headroom so reorder
     or late NACK recovery never evicts the keyframe head.
  2. WIDEN RTP_HISTORY_SIZE so the NACK generator keeps tracking the missing
     packets long enough to re-request them.
  3. PERIODIC re-NACK: aiortc NACKs missing packets once; we add a background
     task per receiver that re-sends NACKs for still-missing packets every
     NACK_INTERVAL until they arrive or age out. This is what actually recovers a
     dropped keyframe-head fragment when the first NACK or the retransmission is
     lost.

All three are tunable via env vars so the next debugging run can sweep them
without code edits:
    BIRDFY_JITTER_CAPACITY   (default 2048, must be power of 2)
    BIRDFY_RTP_HISTORY_SIZE  (default 1024)
    BIRDFY_NACK_INTERVAL_MS  (default 30; 0 disables periodic re-NACK)
    BIRDFY_NACK_MAX_RETRIES  (default 12 re-sends per missing packet)

Importing this module installs the patches as a side effect. Import it ONCE,
before constructing the RTCPeerConnection (i.e. alongside _aioice_patches).
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiortc.rtcrtpreceiver as _recv_mod
import aiortc.rtp as _rtp_mod
from aiortc.jitterbuffer import JitterBuffer

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r, using default %d", name, raw, default)
        return default


JITTER_CAPACITY = _env_int("BIRDFY_JITTER_CAPACITY", 2048)
RTP_HISTORY_SIZE = _env_int("BIRDFY_RTP_HISTORY_SIZE", 1024)
NACK_INTERVAL_MS = _env_int("BIRDFY_NACK_INTERVAL_MS", 30)
NACK_MAX_RETRIES = _env_int("BIRDFY_NACK_MAX_RETRIES", 12)

# Validate capacity is a power of two (aiortc asserts this).
if JITTER_CAPACITY & (JITTER_CAPACITY - 1) != 0:
    logger.warning(
        "BIRDFY_JITTER_CAPACITY=%d is not a power of 2 — falling back to 2048",
        JITTER_CAPACITY,
    )
    JITTER_CAPACITY = 2048


# ---------------------------------------------------------------------------
# Patch 1 + 2: widen jitter buffer and NACK history.
# ---------------------------------------------------------------------------

_orig_jb_init = JitterBuffer.__init__


def _patched_jb_init(self, capacity, prefetch=0, is_video=False):
    # Only widen the *video* buffer; audio's tiny buffer (16) is fine and a huge
    # audio buffer would just add latency.
    if is_video and capacity < JITTER_CAPACITY:
        logger.info(
            "JitterBuffer(video): widening capacity %d -> %d (keyframe-fit)",
            capacity,
            JITTER_CAPACITY,
        )
        capacity = JITTER_CAPACITY
    _orig_jb_init(self, capacity=capacity, prefetch=prefetch, is_video=is_video)


def _install_buffer_patches() -> None:
    if JitterBuffer.__init__ is not _patched_jb_init:
        JitterBuffer.__init__ = _patched_jb_init  # type: ignore[method-assign]

    # NackGenerator.truncate() reads module-global RTP_HISTORY_SIZE from
    # rtcrtpreceiver's namespace (imported there). Bump both the source module and
    # the receiver module's binding so the larger window takes effect everywhere.
    if getattr(_rtp_mod, "RTP_HISTORY_SIZE", None) != RTP_HISTORY_SIZE:
        logger.info(
            "RTP_HISTORY_SIZE: widening %s -> %d (NACK tracking window)",
            getattr(_rtp_mod, "RTP_HISTORY_SIZE", "?"),
            RTP_HISTORY_SIZE,
        )
    _rtp_mod.RTP_HISTORY_SIZE = RTP_HISTORY_SIZE
    # rtcrtpreceiver did `from .rtp import RTP_HISTORY_SIZE`, so it holds its own
    # name bound to the old value — rebind it too.
    if hasattr(_recv_mod, "RTP_HISTORY_SIZE"):
        _recv_mod.RTP_HISTORY_SIZE = RTP_HISTORY_SIZE


# ---------------------------------------------------------------------------
# Patch 3: periodic re-NACK for still-missing packets.
#
# We don't touch _handle_rtp_packet (aiortc's one-shot NACK there still fires on
# first detection — fine, it's an early request). Instead we attach a background
# loop to each video receiver that, every NACK_INTERVAL_MS, asks the receiver's
# NackGenerator what is still missing and re-sends a NACK for it. The generator's
# `missing` set is maintained by aiortc (entries are discarded as packets arrive
# or age past RTP_HISTORY_SIZE), so we just read and re-request.
# ---------------------------------------------------------------------------

# Per-receiver retry bookkeeping: id(receiver) -> {seq: retries_sent}
_renack_tasks: dict[int, asyncio.Task] = {}


def attach_periodic_renack(receiver) -> None:
    """Start a background re-NACK loop for `receiver`. Idempotent per receiver.

    Safe to call from on_track. No-op if periodic re-NACK is disabled
    (BIRDFY_NACK_INTERVAL_MS=0) or if aiortc internals aren't where we expect.
    """
    if NACK_INTERVAL_MS <= 0:
        logger.info("Periodic re-NACK disabled (BIRDFY_NACK_INTERVAL_MS=0)")
        return

    rid = id(receiver)
    if rid in _renack_tasks and not _renack_tasks[rid].done():
        return

    nack_gen = getattr(receiver, "_RTCRtpReceiver__nack_generator", None)
    if nack_gen is None:
        logger.warning(
            "Periodic re-NACK: receiver has no __nack_generator — "
            "aiortc internals changed; skipping"
        )
        return

    interval = NACK_INTERVAL_MS / 1000.0
    retries: dict[int, int] = {}

    async def _loop() -> None:
        logger.info(
            "Periodic re-NACK loop started (interval=%dms max_retries=%d)",
            NACK_INTERVAL_MS,
            NACK_MAX_RETRIES,
        )
        total_renacks = 0
        try:
            while True:
                await asyncio.sleep(interval)

                missing = sorted(getattr(nack_gen, "missing", set()))
                if not missing:
                    # Clear retry bookkeeping for anything no longer missing.
                    if retries:
                        retries.clear()
                    continue

                # Drop retry counters for seqs that recovered.
                missing_set = set(missing)
                for seq in list(retries):
                    if seq not in missing_set:
                        del retries[seq]

                # Re-request only those still under the retry cap.
                to_request = []
                for seq in missing:
                    sent = retries.get(seq, 0)
                    if sent < NACK_MAX_RETRIES:
                        to_request.append(seq)
                        retries[seq] = sent + 1

                if not to_request:
                    continue

                # media_ssrc: the SSRC we're receiving on. Use the most recent
                # active SSRC (the NACK target). active_ssrc maps ssrc->last_seen.
                active = getattr(receiver, "_RTCRtpReceiver__active_ssrc", {})
                if not active:
                    continue
                media_ssrc = next(iter(active.keys()))

                try:
                    await receiver._send_rtcp_nack(media_ssrc, to_request)
                    total_renacks += 1
                    # Occasional re-NACK is normal packet-loss recovery, not an
                    # event worth an INFO line each time — keep per-send detail at
                    # DEBUG and emit a periodic INFO heartbeat so steady-state logs
                    # stay quiet while deep debugging still gets the full picture.
                    logger.debug(
                        "Re-NACK #%d: ssrc=%d requesting %d missing seqs %s%s",
                        total_renacks,
                        media_ssrc,
                        len(to_request),
                        to_request[:16],
                        " ..." if len(to_request) > 16 else "",
                    )
                    if total_renacks % 200 == 0:
                        logger.info(
                            "Re-NACK heartbeat: %d re-NACKs sent so far "
                            "(latest: %d missing seqs)",
                            total_renacks,
                            len(to_request),
                        )
                except Exception as e:
                    logger.debug("Re-NACK send failed (non-fatal): %s", e)
        except asyncio.CancelledError:
            logger.debug("Periodic re-NACK loop cancelled")
            raise

    _renack_tasks[rid] = asyncio.ensure_future(_loop())


def detach_periodic_renack(receiver) -> None:
    """Cancel the re-NACK loop for `receiver`, if any."""
    rid = id(receiver)
    task = _renack_tasks.pop(rid, None)
    if task is not None and not task.done():
        task.cancel()


# ---------------------------------------------------------------------------
# Diagnostic: log SDP-advertised RTCP feedback (nack/pli/fir) for the negotiated
# video codecs. Call after setRemoteDescription with the peer connection.
# ---------------------------------------------------------------------------

def log_video_rtcp_feedback(pc) -> None:
    """Log the rtcp-fb the camera advertised per video codec.

    This tells us whether NACK (and thus retransmission-based keyframe recovery)
    is even on the table, plus pli/fir support — central to the keyframe debug.
    """
    for transceiver in pc.getTransceivers():
        if transceiver.kind != "video":
            continue
        receiver = transceiver.receiver
        codecs = getattr(receiver, "_RTCRtpReceiver__codecs", None)
        if not codecs:
            logger.info("RTCP-FB: video receiver has no negotiated codecs yet")
            continue
        for pt, codec in codecs.items():
            fb = getattr(codec, "rtcpFeedback", []) or []
            fb_str = ", ".join(
                f"{f.type}{(' ' + f.parameter) if f.parameter else ''}" for f in fb
            ) or "(none)"
            has_nack = any(f.type == "nack" and not f.parameter for f in fb)
            logger.info(
                "RTCP-FB pt=%s codec=%s/%s rtcp-fb=[%s] NACK=%s",
                pt,
                codec.name,
                getattr(codec, "clockRate", "?"),
                fb_str,
                "YES" if has_nack else "NO",
            )


def install() -> None:
    """Install the buffer/NACK-window patches. Idempotent."""
    _install_buffer_patches()
    logger.info(
        "aiortc media patches installed: jitter_capacity=%d rtp_history=%d "
        "renack_interval_ms=%d renack_max_retries=%d",
        JITTER_CAPACITY,
        RTP_HISTORY_SIZE,
        NACK_INTERVAL_MS,
        NACK_MAX_RETRIES,
    )


# Install on import so callers only need ``import _aiortc_media_patches``.
install()
