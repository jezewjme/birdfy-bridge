"""
Birdfy WebRTC client — Addx/a4x signaling path.

Connects to the Addx signaling server via WebSocket using a ticket from
GET {localEndpoint}/v1/addx/token/v2, completes SDP offer/answer + ICE
negotiation, receives H264 video, and pipes decoded frames → ffmpeg → RTSP.

WebSocket URL format (from ticket):
  {ticket.signalServer}/{ticket.groupId}/{ticket.role}/{ticket.id}
  ?traceId={ticket.traceId}&time={ticket.time}&sign={ticket.sign}&name=a4x

SDP message format (base64-encoded JSON payload):
  {messageType:"SDP_OFFER", recipientClientId, senderClientId, sessionId,
   messagePayload: base64(json({sdp, type:"offer"})),
   resolution:"auto", viewerType:"netvue_web_sdk", mode:"vicoo"}

ICE candidate format:
  {messageType:"ICE_CANDIDATE", recipientClientId, senderClientId, sessionId,
   messagePayload: base64(json(candidate)), mode:"vicoo"}

The camera acts as the WebRTC master (sends SDP_ANSWER); we are the viewer
(send SDP_OFFER). Heartbeat: send the last cached ICE candidate every
ticket.signalPingInterval seconds (default: 2s).
"""
import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
import uuid

import websockets
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.rtcconfiguration import RTCBundlePolicy
from aiortc.sdp import candidate_from_sdp

# Side-effect import: installs aioice monkey-patches required for the Addx
# camera's STUN/ICE quirks. See _aioice_patches.py for what each patch does.
import _aioice_patches  # noqa: F401
# Side-effect import: installs aiortc RTP receive-path patches (wider video
# jitter buffer + NACK history + periodic re-NACK) so large keyframes aren't
# evicted before their head fragment is recovered. See _aiortc_media_patches.py.
import _aiortc_media_patches
from _rtp_forwarder import forward_video
from _sdp_patches import apply_offer_patches, extract_trickle_candidates

logger = logging.getLogger(__name__)

# SCTP role: aiortc default (is_server=False, SCTP client, sends INIT first).
#
# Earlier this code was patched to is_server=True based on bad guidance ("Chrome
# is SCTP server"). Wireshark capture of the actual successful Edge handshake
# (Birdify with Edge connecting successfully.pcapng) shows the OPPOSITE:
# immediately after DTLS, the BROWSER sends DTLS Application Data (SCTP INIT)
# and the camera responds with INIT-ACK. Without this, both sides sit waiting
# and the camera never starts media — confirmed by 5-10-26 bridge connect.pcapng
# where the camera goes silent (just STUN keepalives) post-DTLS.
#
# Keeping aiortc's default behavior here on purpose; do not re-add the patch.

# How long to wait between decoded frames before declaring the track dead.
# Birdfy camera takes 30s+ to produce a first decodable frame even with PLIs;
# during steady-state, gaps are <1s.
FRAME_TIMEOUT = 90
DATA_CHANNEL_LABEL = os.getenv("BIRDFY_DC_LABEL", "webDataChannel")
DATA_CHANNEL_PROTOCOL = os.getenv("BIRDFY_DC_PROTOCOL", "")
_default_payloads = [
    '{"action":"startLive","resolution":"auto"}',
    '{"action":"startLive"}',
    '{"action":"startlive","resolution":"auto"}',
    '{"action":"startlive"}',
    "startLive",
]
DATA_CHANNEL_PAYLOADS = [
    p.strip()
    for p in os.getenv("BIRDFY_DC_PAYLOADS", "|".join(_default_payloads)).split("|")
    if p.strip()
]


def _build_ws_url(ticket: dict) -> str:
    """Build the Addx signaling WebSocket URL from a ticket."""
    signal_server = ticket["signalServer"]
    group_id = ticket["groupId"]
    role = ticket["role"]
    client_id = ticket["id"]
    trace_id = ticket.get("traceId", "")
    ts = ticket.get("time", "")
    sign = ticket.get("sign", "")

    access_token = ticket.get("accessToken", "")
    url = (
        f"{signal_server}/{group_id}/{role}/{client_id}"
        f"?traceId={trace_id}&time={ts}&sign={sign}"
    )
    if access_token:
        url += f"&accessToken={access_token}"
    url += "&name=a4x"
    # URL carries the HMAC signature in `sign=` and an access token; log only
    # the server + path so DEBUG output is safe to share for bug reports.
    logger.info(
        f"WebSocket URL: {signal_server}/{group_id}/{role}/{client_id} (auth params redacted)"
    )
    return url


def _build_session_id(ticket: dict, a4x_user_id: str) -> str:
    """Build sessionId: web-{userId}-{timestamp}"""
    return f"web-{a4x_user_id}-{int(asyncio.get_event_loop().time() * 1000)}"


def _make_ice_config(ticket: dict) -> RTCConfiguration:
    """Build aiortc RTCConfiguration from ticket ICE servers."""
    servers = [RTCIceServer(urls="stun:stun.l.google.com:19302")]

    ice_servers = ticket.get("iceServer") or ticket.get("iceServers") or []
    for s in ice_servers:
        urls = s.get("url") or s.get("urls") or []
        username = s.get("username", "")
        credential = s.get("credential", "")
        if urls:
            if isinstance(urls, str):
                urls = [urls]
            servers.append(RTCIceServer(urls=urls, username=username, credential=credential))
            logger.debug(f"ICE server: {urls}")

    # MAX_BUNDLE forces all transceivers onto a single ICE/DTLS transport so the
    # SDP offer has matching ufrag/pwd across m-lines. With the default BALANCED
    # policy, video and audio each get their own transport (Connection(0)/(1) in
    # aioice logs) — DTLS lands on one transport while ICE only succeeds on the
    # other, so the handshake never completes against this camera.
    return RTCConfiguration(iceServers=servers, bundlePolicy=RTCBundlePolicy.MAX_BUNDLE)


def _b64_encode(obj) -> str:
    """Base64-encode a JSON-serializable object."""
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _sdp_offer_msg(sdp: str, recipient_id: str, sender_id: str, session_id: str) -> dict:
    """Build the SDP_OFFER WebSocket message."""
    return {
        "messageType": "SDP_OFFER",
        "recipientClientId": recipient_id,
        "senderClientId": sender_id,
        "messagePayload": _b64_encode({"sdp": sdp, "type": "offer"}),
        "sessionId": session_id,
        "resolution": "auto",
        "viewerType": "netvue_web_sdk",
        "mode": "vicoo",
    }


def _ice_candidate_msg(
    candidate_line: str,
    sdp_mid: str,
    sdp_mline_index: int,
    ufrag: str,
    recipient_id: str,
    sender_id: str,
    session_id: str,
) -> dict:
    """Build an ICE_CANDIDATE WebSocket message matching the browser's shape."""
    payload = {
        "candidate": candidate_line,
        "sdpMid": sdp_mid,
        "sdpMLineIndex": sdp_mline_index,
        "usernameFragment": ufrag,
    }
    return {
        "messageType": "ICE_CANDIDATE",
        "recipientClientId": recipient_id,
        "senderClientId": sender_id,
        "sessionId": session_id,
        "messagePayload": _b64_encode(payload),
        "mode": "vicoo",
    }


async def connect_and_stream(
    ticket: dict,
    rtsp_output: str,
    a4x_user_id: str = "",
    serial_number: str = "",
):
    """
    Connect to the Addx WebRTC signaling server and stream to RTSP.

    Args:
        ticket:         Ticket dict from get_addx_ticket()
        rtsp_output:    RTSP push URL (e.g. rtsp://go2rtc:8554/birdfy)
        a4x_user_id:    A4x user ID (from auth data or device node)
        serial_number:  Device serial number (for logging)
    """
    config = _make_ice_config(ticket)
    pc = RTCPeerConnection(configuration=config)
    ffmpeg_state: dict = {"proc": None, "size": None}

    # Tell aiortc we want to receive video (and audio if camera sends it).
    # Without this the SDP offer has no m= lines and the server drops the connection.
    # Order matters: browser HAR shows audio m-line FIRST, then video, then
    # application. Camera SDP_ANSWER mirrors that order. aiortc emits m-lines in
    # addTransceiver order, so we add audio first to match the browser.
    pc.addTransceiver("audio", direction="recvonly")
    pc.addTransceiver("video", direction="recvonly")

    # Create a data channel BEFORE the offer so the SDP includes an m=application
    # section. The camera receives the startLive trigger over this channel — not via
    # the HTTPS device/startlive API (which always returns -3021 for Addx cameras
    # despite the cloud accepting the auth). Confirmed by sniffing my.birdfy.com:
    # the webapp sends DTLS Application Data (SCTP-over-DTLS) immediately after the
    # handshake, then bulk SRTP follows.
    # Mirror Chrome behavior observed on my.birdfy.com:
    # label=webDataChannel with default negotiated=False and auto-assigned stream id.
    control_channel = pc.createDataChannel(
        DATA_CHANNEL_LABEL,
        protocol=DATA_CHANNEL_PROTOCOL,
    )

    @control_channel.on("open")
    def _control_open():
        logger.info(
            "Control channel open: label=%s protocol=%r id=%s ordered=%s negotiated=%s",
            control_channel.label,
            control_channel.protocol,
            control_channel.id,
            control_channel.ordered,
            control_channel.negotiated,
        )
        for idx, msg in enumerate(DATA_CHANNEL_PAYLOADS, start=1):
            try:
                logger.info(
                    "Control channel send [%s/%s]: %s",
                    idx,
                    len(DATA_CHANNEL_PAYLOADS),
                    msg,
                )
                control_channel.send(msg)
            except Exception as e:
                logger.warning(f"Control channel send failed for payload {idx}: {e}")

    @control_channel.on("message")
    def _control_msg(m):
        logger.info(f"Control channel msg ({type(m).__name__}): {m!r}")

    # IDs for signaling messages.
    # sender_id must match ticket["id"] — that is how the signaling server identifies us.
    # (confirmed from PEER_IN recipientClientId matching ticket.id, not the Netvue userID)
    sender_id = str(ticket.get("id") or a4x_user_id or uuid.uuid4().hex[:8])
    session_id = sender_id
    # recipient is the camera's addxSn (its master client ID)
    recipient_id = serial_number or ""

    @pc.on("track")
    def on_track(track):
        logger.info(f"Track received: kind={track.kind}")
        if track.kind == "video":
            # RTP passthrough: hook the video receiver's jitter buffer and pipe
            # depayloaded H264 directly to ffmpeg -c copy. Bypasses aiortc's
            # broken libavcodec decoder for this camera's bitstream.
            receiver = next(
                (t.receiver for t in pc.getTransceivers()
                 if t.kind == "video" and t.receiver is not None),
                None,
            )
            if receiver is None:
                logger.error("Video track received but no video receiver found — falling back to decode path")
                asyncio.ensure_future(_stream_video(track, rtsp_output, ffmpeg_state, pc))
            else:
                # Keep aiortc's track-drain loop running too, otherwise the
                # decoder thread's output queue backs up and may block the
                # decoder worker. _drain() is a no-op consumer.
                asyncio.ensure_future(_drain(track))
                asyncio.ensure_future(
                    forward_video(receiver, rtsp_output, frame_timeout=FRAME_TIMEOUT)
                )
                # Periodic re-NACK: aiortc requests a missing packet only once.
                # Large keyframes (~50-110 packets) can lose their head fragment
                # and never recover before the jitter buffer evicts it; this loop
                # re-requests still-missing seqs until they arrive. See
                # _aiortc_media_patches.py for the full rationale.
                _aiortc_media_patches.attach_periodic_renack(receiver)
                # Still nudge the camera for an early keyframe.
                asyncio.ensure_future(_pli_nudger(pc))
        else:
            asyncio.ensure_future(_drain(track))

    @pc.on("datachannel")
    def on_datachannel(channel):
        logger.info(f"Data channel: {channel.label}")

        @channel.on("open")
        def _open():
            # Signal the camera to start live stream
            msg = json.dumps({"action": "startLive", "resolution": "auto"})
            logger.info(f"Data channel open — sending startLive: {msg}")
            channel.send(msg)

        @channel.on("message")
        def _msg(m):
            logger.debug(f"Data channel message: {m}")

    @pc.on("connectionstatechange")
    async def _state():
        sctp_transport = pc.sctp.transport.state if pc.sctp and pc.sctp.transport else "n/a"
        sctp_port = pc.sctp.port if pc.sctp else "n/a"
        logger.info(
            "WebRTC state -> %s (sctp_port=%s dtls=%s)",
            pc.connectionState,
            sctp_port,
            sctp_transport,
        )

    @pc.on("iceconnectionstatechange")
    async def _ice_state():
        logger.info(f"ICE state -> {pc.iceConnectionState}")

    # Create the SDP offer before opening the WebSocket so it's ready to fire
    # the instant PEER_IN arrives (camera times out in ~5s).
    # The camera DOES need our ICE candidates trickled over the WebSocket — the
    # browser HAR shows ~22 ICE_CANDIDATE messages sent right after SDP_OFFER,
    # then the local-host candidate re-sent every ~2s as a heartbeat. Without
    # these the camera's STUN pair stays IN_PROGRESS forever and never nominates.
    logger.info("Creating SDP offer ...")
    _pre_offer = await pc.createOffer()
    await pc.setLocalDescription(_pre_offer)
    _pre_sdp, _patch_info = apply_offer_patches(pc.localDescription.sdp)
    if _patch_info["sctpmap_injected"]:
        logger.info("SDP patched: injected a=sctpmap:5000 for camera compat")
    if _patch_info["fingerprints_stripped"]:
        logger.info(
            f"SDP patched: stripped {_patch_info['fingerprints_stripped']} "
            "sha-384/sha-512 fingerprint lines (browser only sends sha-256)"
        )

    # Match Chrome/Edge usrsctp defaults (1024 streams) instead of aiortc's 65535.
    # Camera's SCTP stack apparently allocates per-stream state up front and OOMs
    # (or hits a hard cap) when offered 65535. Private aiortc attribute — pin
    # aiortc to a known-good minor range in requirements.txt.
    if pc.sctp is not None:
        pc.sctp._outbound_streams_count = 1024
        pc.sctp._inbound_streams_max = 1024
        logger.info("SCTP: capped OS/MIS to 1024 to match Chrome/Edge")

    _gathered_candidates = extract_trickle_candidates(_pre_sdp)
    _host_candidate = next(
        (c for c in _gathered_candidates if " typ host" in c["candidate"]),
        _gathered_candidates[0] if _gathered_candidates else None,
    )
    logger.info(
        f"Gathered {len(_gathered_candidates)} ICE candidates to trickle "
        f"(host heartbeat: {'yes' if _host_candidate else 'no'})"
    )
    logger.info("Connecting to WebSocket ...")

    url = _build_ws_url(ticket)
    ping_interval = ticket.get("signalPingInterval", 2)
    heartbeat_task: asyncio.Task | None = None

    try:
        async with websockets.connect(
            url,
            extra_headers={"User-Agent": "Mozilla/5.0 (compatible; birdfy-bridge/1.0)"},
            ping_interval=ping_interval,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            logger.info("WebSocket connected to Addx signaling server")

            # master_id is discovered from PEER_IN — camera sends this first
            master_id: list = [recipient_id]  # default; overwritten on PEER_IN
            offer_sent = asyncio.Event()
            connected = asyncio.Event()
            peer_in_received = asyncio.Event()
            answer_received = asyncio.Event()

            async def _peer_in_watchdog():
                try:
                    await asyncio.wait_for(peer_in_received.wait(), timeout=30)
                except asyncio.TimeoutError:
                    logger.warning(
                        "No PEER_IN from camera after 30s — camera may be offline or rebooting. "
                        "Will keep waiting but connection is unlikely."
                    )

            async def _answer_watchdog():
                # The camera should answer within a few seconds. Browser HAR shows the
                # first WS attempt waits ~16s with no answer, then PEER_OUT, then a
                # full reconnect with a new ticket. We mirror that: give it 12s, then
                # close the WS so main() loops with a fresh ticket.
                try:
                    await asyncio.wait_for(answer_received.wait(), timeout=12)
                except asyncio.TimeoutError:
                    logger.warning("No SDP_ANSWER within 12s — closing WS to retry with fresh ticket")
                    await ws.close()

            def _make_candidate_msg(cand: dict) -> dict:
                return _ice_candidate_msg(
                    candidate_line=cand["candidate"],
                    sdp_mid=cand["sdpMid"],
                    sdp_mline_index=cand["sdpMLineIndex"],
                    ufrag=cand["ufrag"],
                    recipient_id=master_id[0],
                    sender_id=sender_id,
                    session_id=session_id,
                )

            async def _send_offer():
                offer_msg = _sdp_offer_msg(
                    sdp=_pre_sdp,
                    recipient_id=master_id[0],
                    sender_id=sender_id,
                    session_id=session_id,
                )
                logger.debug(f"Sending SDP_OFFER to {master_id[0]} (first 300): {json.dumps(offer_msg)[:300]}")
                await ws.send(json.dumps(offer_msg))
                offer_sent.set()
                logger.debug("SDP offer sent")

                # Browser sends all gathered candidates as separate ICE_CANDIDATE
                # messages right after SDP_OFFER. The camera apparently requires
                # this to nominate its STUN pair.
                for cand in _gathered_candidates:
                    try:
                        await ws.send(json.dumps(_make_candidate_msg(cand)))
                    except Exception as e:
                        logger.warning(f"ICE candidate send failed (non-fatal): {e}")
                logger.info(f"Trickled {len(_gathered_candidates)} ICE candidates over WS")

            async def _ice_heartbeat():
                # Browser re-sends the local-host candidate every ~signalPingInterval
                # seconds for the lifetime of the WS. Skip if no host candidate is
                # available (then the burst alone is the best we can do).
                if not _host_candidate:
                    return
                interval = max(1.0, float(ticket.get("signalPingInterval", 2)))
                try:
                    while True:
                        await asyncio.sleep(interval)
                        try:
                            await ws.send(json.dumps(_make_candidate_msg(_host_candidate)))
                        except Exception:
                            return
                except asyncio.CancelledError:
                    return

            asyncio.ensure_future(_peer_in_watchdog())
            asyncio.ensure_future(_answer_watchdog())
            heartbeat_task = asyncio.ensure_future(_ice_heartbeat())

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"Non-JSON WS message: {raw[:200]}")
                    continue

                msg_type = msg.get("messageType") or msg.get("type") or msg.get("signal") or ""
                logger.debug(f"WS [{msg_type}]: {raw[:300]}")

                # Camera announces itself — send our SDP offer on the first PEER_IN.
                # The camera may send a second PEER_IN later in the handshake; the
                # browser HAR shows it just keeps trickling ICE through it, so we do
                # the same. Recovery from a broken handshake is driven by PEER_OUT
                # or by the SDP_ANSWER timeout in main(), not by counting PEER_INs.
                if msg_type == "PEER_IN":
                    payload_b64 = msg.get("messagePayload", "")
                    try:
                        peer_info = json.loads(base64.b64decode(payload_b64).decode())
                        if peer_info.get("role") == "master":
                            master_id[0] = msg.get("senderClientId") or peer_info.get("id") or master_id[0]
                    except Exception:
                        pass

                    if not offer_sent.is_set():
                        peer_in_received.set()
                        logger.info(f"PEER_IN from master {master_id[0]} — sending SDP offer")
                        asyncio.ensure_future(_send_offer())
                    else:
                        logger.debug("Subsequent PEER_IN — ignoring (handshake already in progress)")

                # SDP answer from camera
                elif msg_type in ("SDP_ANSWER", "sdp", "answer") or msg.get("type") == "answer":
                    payload = msg.get("messagePayload") or msg.get("body") or msg
                    # messagePayload is base64-encoded JSON: {"sdp": "...", "type": "answer"}
                    if isinstance(payload, str) and len(payload) > 50:
                        try:
                            decoded = json.loads(base64.b64decode(payload).decode())
                            sdp = decoded.get("sdp")
                        except Exception:
                            sdp = None
                        if not sdp:
                            # Fallback: try direct sdp field
                            sdp = msg.get("sdp") or (
                                msg.get("body", {}).get("sdp") if isinstance(msg.get("body"), dict) else None
                            )
                    elif isinstance(payload, dict):
                        sdp = payload.get("sdp")
                    else:
                        sdp = msg.get("sdp")

                    if sdp:
                        await pc.setRemoteDescription(
                            RTCSessionDescription(sdp=sdp, type="answer")
                        )
                        answer_received.set()
                        logger.info("Remote SDP set (SDP_ANSWER received)")
                        # Log a one-line summary of negotiated video codec so we can
                        # see profile-level-id, packetization-mode, and whether the
                        # camera ships sprop-parameter-sets in-band. Useful for
                        # debugging decode failures.
                        for line in sdp.splitlines():
                            ls = line.strip()
                            if (
                                ls.startswith("a=rtpmap:")
                                or ls.startswith("a=fmtp:")
                                or ls.startswith("a=rtcp-fb:")
                                or ls.startswith("a=ssrc-group:")
                            ):
                                logger.info(f"SDP_ANSWER negotiated: {ls}")
                        # Log the parsed per-codec RTCP feedback (esp. NACK
                        # support) so keyframe-recovery debugging has the camera's
                        # advertised capabilities in the log even if video fails.
                        _aiortc_media_patches.log_video_rtcp_feedback(pc)

                # ICE candidate from camera
                elif msg_type in ("ICE_CANDIDATE", "candidate", "iceCandidate"):
                    payload = msg.get("messagePayload") or msg.get("body") or msg
                    # messagePayload is base64-encoded JSON candidate object
                    cand_str = ""
                    sdp_mid = "0"
                    sdp_mline = 0
                    if isinstance(payload, str) and len(payload) > 10:
                        try:
                            decoded = json.loads(base64.b64decode(payload).decode())
                            cand_str = decoded.get("candidate", "")
                            sdp_mid = decoded.get("sdpMid", "0")
                            sdp_mline = decoded.get("sdpMLineIndex", 0)
                        except Exception:
                            pass
                    elif isinstance(payload, dict):
                        cand_str = payload.get("candidate", "")
                        sdp_mid = payload.get("sdpMid", "0")
                        sdp_mline = payload.get("sdpMLineIndex", 0)

                    if cand_str and "candidate:" in cand_str:
                        try:
                            parsed = candidate_from_sdp(cand_str.split("candidate:", 1)[1])
                            parsed.sdpMid = sdp_mid
                            parsed.sdpMLineIndex = sdp_mline
                            await pc.addIceCandidate(parsed)
                            logger.debug(f"ICE candidate added: {cand_str[:80]}")
                        except Exception as e:
                            logger.warning(f"ICE parse failed: {e} — {cand_str[:80]}")

                # Camera disconnected — break immediately so main() retries fast
                if msg_type == "PEER_OUT":
                    logger.warning("PEER_OUT received — camera disconnected, retrying")
                    break

                # Track connection state
                if pc.connectionState == "connected" and not connected.is_set():
                    connected.set()
                    logger.info("WebRTC connected — streaming")

                if pc.connectionState in ("failed", "closed"):
                    logger.warning(f"WebRTC ended: {pc.connectionState}")
                    break

    except websockets.exceptions.ConnectionClosed as e:
        logger.warning(f"WebSocket closed: {e}")
    except Exception as e:
        logger.error(f"Signaling error: {e}", exc_info=True)
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
        # Stop any periodic re-NACK loops attached to this pc's video receivers
        # so they don't leak across reconnects.
        for transceiver in pc.getTransceivers():
            if transceiver.kind == "video" and transceiver.receiver is not None:
                _aiortc_media_patches.detach_periodic_renack(transceiver.receiver)
        _kill_ffmpeg(ffmpeg_state)
        await pc.close()


async def _stream_video(track, rtsp_output: str, state: dict, pc=None):
    """Decode video frames from aiortc and pipe as rawvideo to ffmpeg → RTSP.

    Sends RTCP PLI to the camera until the first decodable frame arrives. The
    Birdfy camera only emits SPS/PPS at keyframe boundaries, and its default
    keyframe cadence is long enough that aiortc spams "Invalid data found" on
    every inter-frame until a keyframe shows up. A PLI tells the camera to
    emit a keyframe now.
    """
    pli_task = asyncio.ensure_future(_pli_nudger(pc)) if pc is not None else None
    got_valid_frame = False
    try:
        while True:
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=FRAME_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("No video frames for 30s — reconnecting")
                break

            w, h = frame.width, frame.height

            if w == 0 or h == 0:
                # Decoder hasn't received a keyframe yet — skip until it has valid dims
                continue

            if not got_valid_frame:
                got_valid_frame = True
                if pli_task is not None and not pli_task.done():
                    pli_task.cancel()
                logger.info(f"First decoded frame: {w}x{h}")

            if state["size"] != (w, h):
                _kill_ffmpeg(state)
                state["size"] = (w, h)
                logger.info(f"Starting ffmpeg: {w}x{h} -> {rtsp_output}")
                state["proc"] = _start_ffmpeg(w, h, rtsp_output)

            proc = state["proc"]
            if proc is None or proc.poll() is not None:
                logger.warning("ffmpeg died — reconnecting")
                break

            try:
                data = frame.to_ndarray(format="yuv420p")
                proc.stdin.write(data.tobytes())  # type: ignore[union-attr]
            except BrokenPipeError:
                logger.warning("ffmpeg pipe broken — reconnecting")
                break

    except Exception as e:
        logger.error(f"Video stream error: {e}", exc_info=True)
    finally:
        if pli_task is not None and not pli_task.done():
            pli_task.cancel()
        _kill_ffmpeg(state)


async def _pli_nudger(pc):
    """Send RTCP PLI **and** FIR to the video sender every 2s until cancelled.

    The Birdfy camera advertises both `nack pli` and `ccm fir` in its answer
    SDP. Empirically PLI alone is ignored — log capture shows 5+ PLIs sent
    over 10s with no responding IDR. We send both each cycle: PLI via aiortc's
    `_send_rtcp_pli`, and FIR (RFC 5104 §4.3.1) via a hand-built RtcpPsfbPacket
    with fmt=RTCP_PSFB_FIR=4 and an 8-byte FCI (media_ssrc + seq_nr + 3 reserved).

    FIR requires an incrementing seq_nr per RFC; repeats with the same seq_nr
    are no-ops, so we bump it every cycle. aiortc doesn't expose a public
    "request keyframe" API, so we reach into receiver internals.
    """
    from struct import pack
    from aiortc.rtp import RTCP_PSFB_FIR, RtcpPsfbPacket

    fir_seq = 0
    try:
        # Wait briefly for the first RTP packet to populate __active_ssrc
        await asyncio.sleep(0.5)
        while True:
            sent = False
            for transceiver in pc.getTransceivers():
                if transceiver.kind != "video":
                    continue
                receiver = transceiver.receiver
                # name-mangled private: dict[ssrc -> last_seen]
                active = getattr(receiver, "_RTCRtpReceiver__active_ssrc", {})
                rtcp_ssrc = getattr(receiver, "_RTCRtpReceiver__rtcp_ssrc", None)
                for ssrc in list(active.keys()):
                    # PLI
                    try:
                        await receiver._send_rtcp_pli(ssrc)
                        sent = True
                        logger.debug(f"Sent PLI for video ssrc={ssrc}")
                    except Exception as e:
                        logger.debug(f"PLI send failed for ssrc={ssrc}: {e}")
                    # FIR — only if we know our sender SSRC for the header
                    if rtcp_ssrc is not None:
                        try:
                            fci = pack("!LBBBB", ssrc, fir_seq & 0xFF, 0, 0, 0)
                            fir = RtcpPsfbPacket(
                                fmt=RTCP_PSFB_FIR,
                                ssrc=rtcp_ssrc,
                                media_ssrc=0,  # RFC 5104: SHOULD be 0 for FIR
                                fci=fci,
                            )
                            await receiver._send_rtcp(fir)
                            logger.debug(f"Sent FIR seq={fir_seq} for video ssrc={ssrc}")
                        except Exception as e:
                            logger.debug(f"FIR send failed for ssrc={ssrc}: {e}")
            if sent:
                fir_seq = (fir_seq + 1) & 0xFF
            else:
                logger.debug("PLI/FIR nudger: no active video SSRC yet")
            await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        logger.debug("PLI/FIR nudger cancelled")
        raise


def _start_ffmpeg(width: int, height: int, rtsp_output: str) -> subprocess.Popen:
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-pix_fmt", "yuv420p",
        "-s", f"{width}x{height}",
        "-r", "15",
        "-i", "pipe:0",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-g", "30",
        "-b:v", "2M",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        rtsp_output,
    ]
    logger.debug(f"ffmpeg cmd: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=open(tempfile.gettempdir() + "/ffmpeg_birdfy.log", "w"),
    )


def _kill_ffmpeg(state: dict):
    proc = state.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    state["proc"] = None
    state["size"] = None


async def _drain(track):
    """Silently discard audio/other tracks so buffers don't fill."""
    try:
        while True:
            await track.recv()
    except Exception:
        pass
