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
import subprocess
import tempfile
import uuid

import websockets
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.rtcconfiguration import RTCBundlePolicy

from birdfy_api import start_live
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

# How long to wait between frames before declaring the track dead
FRAME_TIMEOUT = 30


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


def _ice_candidate_msg(candidate, recipient_id: str, sender_id: str, session_id: str) -> dict:
    """Build the ICE_CANDIDATE WebSocket message."""
    # candidate is an aiortc RTCIceCandidate
    cand_dict = {
        "candidate": f"candidate:{candidate.foundation} {candidate.component} "
                     f"{candidate.protocol} {candidate.priority} "
                     f"{candidate.ip} {candidate.port} typ {candidate.type}",
        "sdpMid": candidate.sdpMid or "0",
        "sdpMLineIndex": candidate.sdpMLineIndex or 0,
    }
    return {
        "messageType": "ICE_CANDIDATE",
        "recipientClientId": recipient_id,
        "senderClientId": sender_id,
        "sessionId": session_id,
        "messagePayload": _b64_encode(cand_dict),
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
    pc.addTransceiver("video", direction="recvonly")
    pc.addTransceiver("audio", direction="recvonly")

    # Create a data channel BEFORE the offer so the SDP includes an m=application
    # section. The camera receives the startLive trigger over this channel — not via
    # the HTTPS device/startlive API (which always returns -3021 for Addx cameras
    # despite the cloud accepting the auth). Confirmed by sniffing my.birdfy.com:
    # the webapp sends DTLS Application Data (SCTP-over-DTLS) immediately after the
    # handshake, then bulk SRTP follows.
    control_channel = pc.createDataChannel("a4xControl")

    @control_channel.on("open")
    def _control_open():
        msg = json.dumps({"action": "startLive", "resolution": "auto"})
        logger.info(f"Control channel open — sending startLive: {msg}")
        control_channel.send(msg)

    @control_channel.on("message")
    def _control_msg(m):
        logger.info(f"Control channel msg: {m!r}")

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
        logger.info(f"WebRTC state -> {pc.connectionState}")

    # Fire startlive ONCE when ICE reaches "completed". Calling earlier (during
    # ticket setup) returns -3021 DEVICE_NO_RESPONSE because the camera's WebRTC
    # stack isn't yet bound to the cloud session. Without a successful startlive
    # the camera completes WebRTC + DTLS but never pushes RTP.
    startlive_fired = {"v": False}

    @pc.on("iceconnectionstatechange")
    async def _ice_state():
        logger.info(f"ICE state -> {pc.iceConnectionState}")
        if pc.iceConnectionState == "completed" and not startlive_fired["v"]:
            startlive_fired["v"] = True
            await start_live(ticket)

    url = _build_ws_url(ticket)
    ping_interval = ticket.get("signalPingInterval", 2)
    cached_candidate_str: list = [None]  # mutable container

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

            async def _send_offer():
                offer = await pc.createOffer()
                await pc.setLocalDescription(offer)
                offer_msg = _sdp_offer_msg(
                    sdp=pc.localDescription.sdp,
                    recipient_id=master_id[0],
                    sender_id=sender_id,
                    session_id=session_id,
                )
                logger.debug(f"Sending SDP_OFFER to {master_id[0]} (first 300): {json.dumps(offer_msg)[:300]}")
                await ws.send(json.dumps(offer_msg))
                offer_sent.set()

            # Set up ICE candidate handler (queues until offer is sent)
            @pc.on("icecandidate")
            async def on_ice_candidate(candidate):
                if candidate and ws.close_code is None:
                    await offer_sent.wait()  # don't send ICE before offer
                    msg = _ice_candidate_msg(candidate, master_id[0], sender_id, session_id)
                    cached_candidate_str[0] = json.dumps(msg)
                    await ws.send(cached_candidate_str[0])
                    logger.debug(f"Sent ICE candidate: {str(candidate)[:80]}")

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"Non-JSON WS message: {raw[:200]}")
                    continue

                msg_type = msg.get("messageType") or msg.get("type") or msg.get("signal") or ""
                logger.debug(f"WS [{msg_type}]: {raw[:300]}")

                # Camera announces itself — send our SDP offer in response
                if msg_type == "PEER_IN" and not offer_sent.is_set():
                    payload_b64 = msg.get("messagePayload", "")
                    try:
                        peer_info = json.loads(base64.b64decode(payload_b64).decode())
                        if peer_info.get("role") == "master":
                            master_id[0] = msg.get("senderClientId") or peer_info.get("id") or master_id[0]
                            logger.info(f"PEER_IN from master {master_id[0]} — sending SDP offer")
                    except Exception:
                        logger.info(f"PEER_IN received — sending SDP offer")
                    asyncio.ensure_future(_send_offer())

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
                        logger.info("Remote SDP set (SDP_ANSWER received)")

                # ICE candidate from camera
                elif msg_type in ("ICE_CANDIDATE", "candidate", "iceCandidate"):
                    payload = msg.get("messagePayload") or msg.get("body") or msg
                    # messagePayload is base64-encoded JSON candidate object
                    if isinstance(payload, str) and len(payload) > 10:
                        try:
                            decoded = json.loads(base64.b64decode(payload).decode())
                            cand_str = decoded.get("candidate", "")
                            sdp_mid = decoded.get("sdpMid", "0")
                            sdp_mline = decoded.get("sdpMLineIndex", 0)
                        except Exception:
                            cand_str = ""
                    elif isinstance(payload, dict):
                        cand_str = payload.get("candidate", "")
                        sdp_mid = payload.get("sdpMid", "0")
                        sdp_mline = payload.get("sdpMLineIndex", 0)
                    else:
                        cand_str = ""

                    if cand_str and "candidate:" in cand_str:
                        try:
                            parsed = candidate_from_sdp(cand_str.split("candidate:", 1)[1])
                            parsed.sdpMid = sdp_mid
                            parsed.sdpMLineIndex = sdp_mline
                            await pc.addIceCandidate(parsed)
                            logger.debug(f"ICE candidate added: {cand_str[:80]}")
                        except Exception as e:
                            logger.warning(f"ICE parse failed: {e} — {cand_str[:80]}")

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
                proc.stdin.write(data.tobytes())
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
