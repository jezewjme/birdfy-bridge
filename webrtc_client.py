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
import aioice.ice
import aioice.stun as stun
from aioice.candidate import candidate_priority

logger = logging.getLogger(__name__)

# --- MONKEY PATCH AIOICE + AIORTC FOR ADDX CAMERAS ---
# Bug 1 — wrong ufrag in outgoing BINDING_REQs (intermittent):
#   Camera sometimes sends USERNAME='ours:stale_ufrag' where stale_ufrag != its SDP ufrag.
#   Original aioice rejects with 400; camera never completes ICE on its side.
#
# Bug 2 — aioice nominates once then stops (ice.py:850, ice.py:880-940):
#   After our first USE-CANDIDATE check succeeds, aioice never sends another. If the
#   camera missed/rejected that single check, it sits in "checking" forever, hammering
#   us with BINDING_REQs at ~50ms intervals. THE HAMMER below sends a fresh
#   BINDING_REQ + USE-CANDIDATE on every camera ping, sidestepping aioice's state
#   machine entirely.
#
# Bug 3 — camera says setup:active but never sends DTLS ClientHello:
#   Camera's ICE stays "checking" → never initiates DTLS. We force aiortc to be the
#   DTLS client so WE send the ClientHello. If the camera firmware is libwebrtc-derived
#   (as Tuya/Vicohome/Hisilicon stacks typically are), it has a DTLS server ready
#   regardless of the SDP role declaration.
_orig_request_received = aioice.ice.Connection.request_received
_ufrag_done: set = set()  # connections where we've already updated remote_username

def _patched_request_received(self, message, addr, protocol, raw_data):
    if message.message_method != stun.Method.BINDING:
        return _orig_request_received(self, message, addr, protocol, raw_data)

    # 1. Update remote ufrag on first mismatch (camera's stale-session bug)
    username = message.attributes.get("USERNAME", "")
    conn_id = id(self)
    if username and ":" in username and conn_id not in _ufrag_done:
        their_ufrag = username.split(":", 1)[1]
        if self.remote_username and self.remote_username != their_ufrag:
            logger.warning(
                f"aioice: ufrag mismatch — SDP={self.remote_username!r} "
                f"camera={their_ufrag!r}; updating remote_username"
            )
            self.remote_username = their_ufrag
            _ufrag_done.add(conn_id)

    # 2. Diagnostic: log pair state for this addr
    pair_state = "no-pair"
    for p in self._check_list:
        if p.remote_addr == addr:
            pair_state = (
                f"{p.state.name} nom={p.nominated} "
                f"task={'set' if p.task else 'none'}"
            )
            break
    logger.debug(
        f"camera REQ from {addr} user={username!r} "
        f"use_cand={'USE-CANDIDATE' in message.attributes} pair={pair_state}"
    )

    # 3. Always respond SUCCESS (signed with our local_password — adds MESSAGE-INTEGRITY + FINGERPRINT)
    response = stun.Message(
        message_method=stun.Method.BINDING,
        message_class=stun.Class.RESPONSE,
        transaction_id=message.transaction_id,
    )
    response.attributes["XOR-MAPPED-ADDRESS"] = addr
    if self.local_password:
        response.add_message_integrity(self.local_password.encode("utf8"))
    protocol.send_stun(response, addr)

    # 4. THE HAMMER: fire a fresh USE-CANDIDATE on every camera ping.
    # Bypasses aioice's "nominate once then stop" behavior so the camera always has
    # a fresh, valid USE-CANDIDATE check arriving with the correct ufrag and HMAC.
    if self.ice_controlling and self.remote_username and self.remote_password:
        try:
            component = protocol.local_candidate.component
            req = stun.Message(
                message_method=stun.Method.BINDING,
                message_class=stun.Class.REQUEST,
            )
            req.attributes["USERNAME"] = f"{self.remote_username}:{self.local_username}"
            req.attributes["PRIORITY"] = candidate_priority(component, "prflx")
            req.attributes["ICE-CONTROLLING"] = self._tie_breaker
            req.attributes["USE-CANDIDATE"] = None  # flag attr
            req.add_message_integrity(self.remote_password.encode("utf8"))
            protocol.send_stun(req, addr)
        except Exception as e:
            logger.debug(f"hammer failed (non-fatal): {e}")

    # 5. Best-effort triggered-check (no-op once pair is SUCCEEDED)
    try:
        if self._check_list:
            self.check_incoming(message, addr, protocol)
    except Exception:
        pass

aioice.ice.Connection.request_received = _patched_request_received

# NOTE: DTLS force-client patch was removed 2026-05-04.
# It was needed only because BUNDLE was broken (BALANCED policy created two transports
# and DTLS was being attached to the one whose ICE never completed, so the camera never
# sent ClientHello). With bundlePolicy=MAX_BUNDLE there's a single transport and ICE
# completes cleanly, so the camera honors its SDP setup:active role and sends ClientHello
# as expected. Forcing our side to client made aiortc reject the camera's ClientHello
# with a Fatal/Unexpected Message alert.
# --------------------------------------------

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

# How long to wait between frames before declaring the track dead
FRAME_TIMEOUT = 30
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
    logger.info(f"WebSocket URL: {url[:140]}...")
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


def _extract_candidates_from_sdp(sdp: str) -> list[dict]:
    """Parse a=candidate: lines out of an SDP and group them by m-line.

    Returns one dict per candidate: {candidate, sdpMid, sdpMLineIndex, ufrag}.
    With MAX_BUNDLE the same candidates appear under each m=, but the browser
    only trickles candidates with sdpMLineIndex=0 (mid="0"), so we mirror that.
    """
    out: list[dict] = []
    sdp_mline = -1
    sdp_mid = "0"
    ufrag = ""
    for raw_line in sdp.splitlines():
        line = raw_line.strip()
        if line.startswith("m="):
            sdp_mline += 1
            # mid will follow in the same m-section; reset
            sdp_mid = str(sdp_mline)
        elif line.startswith("a=mid:"):
            sdp_mid = line[len("a=mid:"):].strip()
        elif line.startswith("a=ice-ufrag:"):
            ufrag = line[len("a=ice-ufrag:"):].strip()
        elif line.startswith("a=candidate:") and sdp_mline == 0:
            cand_no_prefix = line[len("a="):]  # "candidate:..."
            out.append({
                "candidate": cand_no_prefix,
                "sdpMid": sdp_mid,
                "sdpMLineIndex": 0,
                "ufrag": ufrag,
            })
    return out




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
            asyncio.ensure_future(_stream_video(track, rtsp_output, ffmpeg_state))
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
    _pre_sdp = pc.localDescription.sdp
    # Camera's SDP parser requires an explicit a=sctpmap line alongside a=sctp-port;
    # without it SDP_ANSWER never returns.
    if "a=sctp-port:5000" in _pre_sdp and "a=sctpmap:5000" not in _pre_sdp:
        _pre_sdp = _pre_sdp.replace(
            "a=sctp-port:5000\r\n",
            "a=sctp-port:5000\r\na=sctpmap:5000 webrtc-datachannel 1024\r\n",
        )
        logger.info("SDP patched: injected a=sctpmap:5000 for camera compat")

    # Match Chrome/Edge usrsctp defaults (1024 streams) instead of aiortc's 65535.
    if pc.sctp is not None:
        pc.sctp._outbound_streams_count = 1024
        pc.sctp._inbound_streams_max = 1024
        logger.info("SCTP: capped OS/MIS to 1024 to match Chrome/Edge")

    # Strip non-sha-256 DTLS fingerprints. Camera's SDP parser picks a non-sha-256
    # line whose hash won't match the cert observed during the handshake; DTLS still
    # completes (it doesn't verify) but every record afterwards is silently dropped,
    # including the SCTP INIT. Browsers ship only sha-256, so do the same.
    _sdp_lines = _pre_sdp.split("\r\n")
    _filtered = [ln for ln in _sdp_lines if not ln.startswith("a=fingerprint:sha-384")
                 and not ln.startswith("a=fingerprint:sha-512")]
    if len(_filtered) != len(_sdp_lines):
        _pre_sdp = "\r\n".join(_filtered)
        logger.info(
            f"SDP patched: stripped {len(_sdp_lines) - len(_filtered)} sha-384/sha-512 "
            "fingerprint lines (browser only sends sha-256)"
        )
    _gathered_candidates = _extract_candidates_from_sdp(_pre_sdp)
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
            additional_headers={"User-Agent": "Mozilla/5.0 (compatible; birdfy-bridge/1.0)"},
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
        _kill_ffmpeg(ffmpeg_state)
        await pc.close()


async def _stream_video(track, rtsp_output: str, state: dict):
    """Decode video frames from aiortc and pipe as rawvideo to ffmpeg → RTSP."""
    try:
        while True:
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=FRAME_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("No video frames for 30s — reconnecting")
                break

            w, h = frame.width, frame.height

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
        _kill_ffmpeg(state)


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
