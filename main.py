"""
Birdfy RTSP bridge — main entry point.

Auth flow (reverse-engineered from my.birdfy.com web app, confirmed working):
  1. POST https://localweb.nvts.co/v1/users/login/v2 → token, userID, region, localEndpoint
  2. GET  {localEndpoint}/v1/devices/v3              → device list (find your camera by serial)
  3a. If device.onAddx == True (Birdfy Feeder Bamboo, Feeder, Cam, etc.):
      GET {localEndpoint}/v1/addx/token/v2 → ticket{signalServer, groupId, role, id, iceServer, ...}
      WebSocket to signaling URL → SDP offer/answer (mode: vicoo)
      Receive H264 → ffmpeg → RTSP push
  3b. If device.onAddx == False:
      POST {localEndpoint}/devices/{sn}/play provider=KVS_WEBRTC → AWS KVS credentials
      Use AWS Kinesis Video Streams WebRTC SDK (not yet implemented here)

Environment variables:
  BIRDFY_EMAIL         Netvue/Birdfy account email
  BIRDFY_PASSWORD      Netvue/Birdfy account password (plain text; MD5'd internally)
  DEVICE_ID            Camera serial number (e.g. "5372540233101051")
  RTSP_OUTPUT          RTSP push URL (default: rtsp://frigate:8554/birdfy)
  LOG_LEVEL            DEBUG / INFO / WARNING (default: INFO)

  --- Optional overrides for NVS signing ---
  NVS_UCID             App client ID (default: 513774810c)
  NVS_UDID             Device UUID for signing (auto-generated if not set)
"""
import asyncio
import logging
import os
import sys

from birdfy_api import get_addx_ticket, get_devices, login
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


async def run_once():
    # Step 1: Authenticate
    logger.info(f"Authenticating as {BIRDFY_EMAIL} ...")
    auth_data = await login(BIRDFY_EMAIL, BIRDFY_PASSWORD)
    token = auth_data["token"]
    user_id = str(auth_data.get("userID", ""))
    logger.info(f"Authenticated — userID={user_id} region={auth_data.get('region')}")

    # Step 2: Find the target device
    logger.info(f"Fetching device list ...")
    devices = await get_devices(auth_data)

    target = None
    for device in devices:
        if device.get("serialNumber") == DEVICE_ID:
            target = device
            break

    if target is None:
        available = [f"{d.get('serialNumber')} ({d.get('name')})" for d in devices]
        raise RuntimeError(
            f"Device {DEVICE_ID!r} not found. Available devices: {available}\n"
            "Set DEVICE_ID to one of the serial numbers above."
        )

    logger.info(
        f"Device found: {target.get('name')!r} sn={target['serialNumber']} "
        f"onAddx={target.get('onAddx')} region={target.get('region')}"
    )

    on_addx = target.get("onAddx", False)

    if on_addx:
        # Step 3a: Addx WebRTC path
        device_region = target.get("region") or auth_data.get("region")
        logger.info(f"Device uses Addx WebRTC — fetching ticket (region={device_region}) ...")
        ticket = await get_addx_ticket(auth_data, device_region=device_region)

        a4x_user_id = str(auth_data.get("userID", ""))
        serial = target["serialNumber"]

        logger.info(f"Connecting to Addx WebRTC → RTSP output: {RTSP_OUTPUT}")
        await connect_and_stream(
            ticket=ticket,
            rtsp_output=RTSP_OUTPUT,
            a4x_user_id=a4x_user_id,
            serial_number=serial,
        )
    else:
        # Step 3b: KVS WebRTC path (not yet implemented)
        # The camera does not use Addx. It uses AWS Kinesis Video Streams.
        # To implement: call get_stream_play(auth_data, target, provider="KVS_WEBRTC")
        # then use boto3 + KVS Signaling client with the returned AWS credentials.
        logger.error(
            "Device is NOT an Addx device (onAddx=False). "
            "KVS WebRTC path is not yet implemented. "
            "Device details: " + str({k: target.get(k) for k in
                ['serialNumber', 'name', 'onAddx', 'region']})
        )
        raise RuntimeError(
            "KVS WebRTC (non-Addx) path not yet implemented. "
            "Only Addx devices (Birdfy Feeder Bamboo, Feeder, Cam) are currently supported."
        )


async def main():
    retry_delay = 10
    while True:
        try:
            await run_once()
            logger.warning("Session ended cleanly — reconnecting")
        except Exception as e:
            logger.error(f"Bridge error: {e}", exc_info=(log_level == "DEBUG"))

        logger.info(f"Waiting {retry_delay}s before retry ...")
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 120)  # exponential backoff, max 2 min


if __name__ == "__main__":
    asyncio.run(main())
