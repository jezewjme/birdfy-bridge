"""
Birdfy WebRTC client.

Connects to the Netvue / smartvideogo.com signaling server via WebSocket,
completes SDP offer/answer + ICE negotiation, receives H264 video,
and pipes decoded frames → ffmpeg → RTSP output URL.
"""
import asyncio
import hashlib
import json
import logging
import subprocess
import time
import uuid

import websockets
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

logger = logging.getLogger(__name__)

# How long to wait between frames before declaring the track dead
FRAME_TIMEOUT = 30


def _build_signaling_url(
    device_id: str,
    server_id: str,
    client_id: str,
    access_token: str,
    camera_name: str,
) -> str:
    ts = int(time.time() * 1000)
    # sign algorithm observed in browser — MD5 of concatenated fields
    # TODO: update if ws connection is rejected with auth errors
    sign = hashlib.md5(f"{device_id}{access_token}{ts}".encode()).hexdigest()
    trace = f"webrtc-{uuid.uuid4().hex[:8]}"

    url = (
        f"wss://p-signal-{server_id}.smartvideogo.com"
        f"/{device_id}/viewer/{client_id}"
        f"?traceId={trace}&time={ts}&sign={sign}"
        f"&accessToken={access_token}&name={camera_name}"
    )
    logger.info(f"Signaling URL (truncated): {url[:140]}…")
    return url


def _make_ice_config(ice_servers: list | None) -> RTCConfiguration:
    """Build aiortc ICE config from a list of TURN/STUN server dicts (if returned by API)."""
    servers = [RTCIceServer(urls="stun:stun.l.google.com:19302")]

    if ice_servers:
        for s in ice_servers:
            urls = s.get("urls") or s.get("url") or []
            username = s.get("username") or s.get("user") or ""
            credential = s.get("credential") or s.get("password") or ""
            if urls:
                servers.append(RTCIceServer(urls=urls, username=username, credential=credential))
                logger.info(f"ICE server: {urls}")

    return RTCConfiguration(iceServers=servers)


async def connect_and_stream(
    device_id: str,
    server_id: str,
    client_id: str,
    access_token: str,
    camera_name: str,
    rtsp_output: str,
    ice_servers: list | None = None,
):
    config = _make_ice_config(ice_servers)
    pc = RTCPeerConnection(configuration=config)
    ffmpeg_state: dict = {"proc": None, "size": None}

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
            msg = json.dumps({"action": "startLive", "resolution": "FHD"})
            logger.info(f"→ data channel: {msg}")
            channel.send(msg)

        @channel.on("message")
        def _msg(m):
            logger.debug(f"← data channel: {m}")

    @pc.on("connectionstatechange")
    async def _state():
        logger.info(f"WebRTC state → {pc.connectionState}")

    url = _build_signaling_url(device_id, server_id, client_id, access_token, camera_name)

    try:
        async with websockets.connect(
            url,
            additional_headers={"User-Agent": "Mozilla/5.0 (compatible; birdfy-bridge/1.0)"},
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            logger.info("WebSocket connected")

            # Create and send offer
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)

            offer_msg = json.dumps({
                "signal": "sdp",
                "type": "offer",
                "body": {"sdp": pc.localDescription.sdp},
            })
            logger.debug(f"→ offer SDP (first 300): {offer_msg[:300]}")
            await ws.send(offer_msg)

            # Process signaling messages
            connected = asyncio.Event()

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"Non-JSON WS message: {raw[:200]}")
                    continue

                sig = msg.get("signal") or msg.get("type") or ""
                logger.debug(f"← WS [{sig}]: {raw[:300]}")

                # SDP answer
                if sig in ("sdp", "answer") or msg.get("type") == "answer":
                    body = msg.get("body") or msg
                    sdp = (body.get("sdp")
                           or body.get("answer", {}).get("sdp")
                           or body.get("answerSdp"))
                    if sdp:
                        await pc.setRemoteDescription(
                            RTCSessionDescription(sdp=sdp, type="answer")
                        )
                        logger.info("Remote SDP set (answer received)")

                # ICE candidate
                elif sig in ("candidate", "iceCandidate") or msg.get("type") == "candidate":
                    body = msg.get("body") or msg
                    cand = body.get("candidate") or body
                    if isinstance(cand, dict):
                        cand_str = cand.get("candidate", "")
                    else:
                        cand_str = str(cand)

                    if cand_str and "candidate:" in cand_str:
                        from aiortc.sdp import candidate_from_sdp
                        try:
                            parsed = candidate_from_sdp(cand_str.split("candidate:", 1)[1])
                            parsed.sdpMid = (body if isinstance(body, dict) else {}).get("sdpMid", "0")
                            parsed.sdpMLineIndex = (body if isinstance(body, dict) else {}).get("sdpMLineIndex", 0)
                            await pc.addIceCandidate(parsed)
                            logger.debug(f"ICE candidate added: {cand_str[:80]}")
                        except Exception as e:
                            logger.warning(f"ICE candidate parse failed: {e} — raw: {cand_str[:120]}")

                # Track connection state
                if pc.connectionState == "connected" and not connected.is_set():
                    connected.set()
                    logger.info("✓ WebRTC connected — streaming")

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

            # (Re)start ffmpeg if this is the first frame or resolution changed
            if state["size"] != (w, h):
                _kill_ffmpeg(state)
                state["size"] = (w, h)
                logger.info(f"Starting ffmpeg: {w}×{h} → {rtsp_output}")
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
        # Input: raw YUV420p frames from stdin
        "-f", "rawvideo",
        "-pix_fmt", "yuv420p",
        "-s", f"{width}x{height}",
        "-r", "10",
        "-i", "pipe:0",
        # Encode
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-g", "20",           # keyframe every 2s at 10fps
        "-b:v", "2M",
        # Output
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        rtsp_output,
    ]
    logger.debug(f"ffmpeg cmd: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=open("/tmp/ffmpeg_birdfy.log", "w"),
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
