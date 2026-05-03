"""
Birdfy RTSP bridge — main entry point.

Auth flow:
  1. POST to Netvue API → get accessToken
  2. GET stream info → get signaling server + viewer client ID
  3. WebSocket → WebRTC SDP exchange
  4. Receive H264 video → ffmpeg → RTSP push to go2rtc

Environment variables:
  BIRDFY_EMAIL         Netvue/Birdfy account email
  BIRDFY_PASSWORD      Netvue/Birdfy password (plain text)
  DEVICE_ID            Camera device ID (hex string)
  RTSP_OUTPUT          Where to push RTSP (default: rtsp://frigate:8554/birdfy)
  CAMERA_NAME          Camera name used in signaling URL (default: A4xCamera)
  LOG_LEVEL            DEBUG / INFO / WARNING (default: INFO)

  --- Manual overrides if API field extraction fails ---
  SERVER_ID            Signaling server ID (e.g. "1", "us01")
  CLIENT_ID            Viewer client UUID (auto-generated if not set)
  SKIP_STREAM_INFO     Set to "1" to skip get_stream_info() and use overrides above
  NETVUE_API           Override base API URL (default: https://user-na.netvue.com)
"""
import asyncio
import logging
import os
import sys
import uuid

from birdfy_api import get_stream_info, login
from webrtc_client import connect_and_stream

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("main")

BIRDFY_EMAIL    = os.environ["BIRDFY_EMAIL"]
BIRDFY_PASSWORD = os.environ["BIRDFY_PASSWORD"]
DEVICE_ID       = os.environ["DEVICE_ID"]
RTSP_OUTPUT     = os.getenv("RTSP_OUTPUT", "rtsp://frigate:8554/birdfy")
CAMERA_NAME     = os.getenv("CAMERA_NAME", "A4xCamera")
SKIP_STREAM_INFO = os.getenv("SKIP_STREAM_INFO", "0") == "1"


def _extract(data: dict, *keys, default=None):
    """Try multiple field name candidates, return first match."""
    for k in keys:
        if k in data and data[k]:
            return data[k]
    return default


async def run_once():
    # ── Step 1: Authenticate ───────────────────────────────────────────
    logger.info(f"Authenticating as {BIRDFY_EMAIL} …")
    auth_data = await login(BIRDFY_EMAIL, BIRDFY_PASSWORD)
    logger.info(f"Auth response keys: {list(auth_data.keys()) if isinstance(auth_data, dict) else type(auth_data)}")

    access_token = _extract(
        auth_data,
        "accessToken", "access_token", "token", "jwt",
    )
    if not access_token:
        logger.error(f"Full auth response: {auth_data}")
        raise RuntimeError(
            "Auth succeeded but no accessToken found in response. "
            "Check the 'Full auth response' log line above and update field extraction."
        )

    logger.info(f"Access token obtained (length={len(access_token)})")

    # ── Step 2: Get signaling parameters ──────────────────────────────
    if SKIP_STREAM_INFO:
        # Manual override mode
        server_id = os.environ.get("SERVER_ID", "1")
        client_id = os.environ.get("CLIENT_ID", uuid.uuid4().hex)
        signal_token = access_token
        ice_servers = None
        logger.info(f"Skipping stream info — using SERVER_ID={server_id} CLIENT_ID={client_id}")
    else:
        logger.info("Fetching stream info …")
        stream_data = await get_stream_info(DEVICE_ID, access_token)
        logger.info(f"Stream data: {stream_data}")

        server_id = _extract(
            stream_data,
            "signalServerId", "serverId", "server_id", "server",
            "signalServer",
            default=os.environ.get("SERVER_ID", "1"),
        )
        client_id = _extract(
            stream_data,
            "clientId", "viewerId", "viewer_id", "viewerClientId",
            default=os.environ.get("CLIENT_ID", uuid.uuid4().hex),
        )
        signal_token = _extract(
            stream_data,
            "signalToken", "liveToken", "streamToken", "webrtcToken",
            "accessToken",
            default=access_token,
        )
        ice_servers = _extract(
            stream_data,
            "iceServers", "turnServers", "turnUrls",
            default=None,
        )

        logger.info(f"server_id={server_id}  client_id={client_id}")
        if ice_servers:
            logger.info(f"ICE servers from API: {ice_servers}")

    # ── Step 3: Connect WebRTC and stream ─────────────────────────────
    logger.info(f"Connecting WebRTC → RTSP output: {RTSP_OUTPUT}")
    await connect_and_stream(
        device_id=DEVICE_ID,
        server_id=str(server_id),
        client_id=str(client_id),
        access_token=signal_token,
        camera_name=CAMERA_NAME,
        rtsp_output=RTSP_OUTPUT,
        ice_servers=ice_servers,
    )


async def main():
    retry_delay = 10
    while True:
        try:
            await run_once()
            logger.warning("Session ended cleanly — reconnecting")
        except Exception as e:
            logger.error(f"Bridge error: {e}", exc_info=(log_level == "DEBUG"))

        logger.info(f"Waiting {retry_delay}s before retry …")
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 120)  # exponential backoff, max 2 min


if __name__ == "__main__":
    asyncio.run(main())
