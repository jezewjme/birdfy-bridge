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
  DEVICE_ID            Camera serial number (e.g. "5372540233101051"). Optional — defaults to first device on the account.
  RTSP_OUTPUT          Full RTSP push URL. If unset, built from RTSP_HOST + RTSP_PATH.
  RTSP_HOST            RTSP server host:port (default: localhost:8554) — used only if RTSP_OUTPUT is unset.
  RTSP_PATH            RTSP stream path (default: birdfy) — used only if RTSP_OUTPUT is unset.
  LOG_LEVEL            DEBUG / INFO / WARNING (default: INFO)
  LOG_FILE             Path to file for logging output (default: birdfy-bridge.log)

  --- Optional overrides for NVS signing ---
  NVS_UCID             App client ID (default: 513774810c)
  NVS_UDID             Device UUID for signing (auto-generated if not set)
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

from birdfy_api import get_addx_ticket, get_devices, login, select_single_device, stop_live
from webrtc_client import connect_and_stream

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
log_file = os.getenv("LOG_FILE", "birdfy-bridge.log")

log_handlers = [logging.StreamHandler(stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False))]
if log_file:
    log_path = Path(log_file)
    if log_path.parent:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handlers.append(logging.FileHandler(log_path, mode="a", encoding="utf-8"))

logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=log_handlers,
    force=True,
)
logger = logging.getLogger("main")

BIRDFY_EMAIL    = os.environ["BIRDFY_EMAIL"]
BIRDFY_PASSWORD = os.environ["BIRDFY_PASSWORD"]
DEVICE_ID       = os.getenv("DEVICE_ID", "")
RTSP_OUTPUT     = os.getenv("RTSP_OUTPUT") or (
    f"rtsp://{os.getenv('RTSP_HOST', 'localhost:8554')}/{os.getenv('RTSP_PATH', 'birdfy')}"
)


async def run_once():
    # Step 1: Authenticate
    logger.info(f"Authenticating as {BIRDFY_EMAIL} ...")
    auth_data = await login(BIRDFY_EMAIL, BIRDFY_PASSWORD)
    user_id = str(auth_data.get("userID", ""))
    logger.info(f"Authenticated — userID={user_id} region={auth_data.get('region')}")

    # Step 2: Find the target device
    logger.info("Fetching device list ...")
    devices = await get_devices(auth_data)

    target = None
    if DEVICE_ID:
        for device in devices:
            if device.get("serialNumber") == DEVICE_ID or device.get("addxSn") == DEVICE_ID:
                target = device
                break

    if target is None:
        if DEVICE_ID:
            available = [f"{d.get('serialNumber')} / addxSn={d.get('addxSn')} ({d.get('deviceName')})" for d in devices]
            logger.warning(
                f"Device {DEVICE_ID!r} not found — falling back to first device. "
                f"Available: {available}"
            )
        if not devices:
            raise RuntimeError("No devices found on this account.")
        target = devices[0]
        logger.info(f"Using device: {target.get('deviceName')!r} sn={target.get('serialNumber')}")

    logger.info(
        f"Device found: {target.get('deviceName')!r} sn={target['serialNumber']} "
        f"addxSn={target.get('addxSn')} onAddx={target.get('onAddx')} region={target.get('region')}"
    )

    on_addx = target.get("onAddx", False)

    if on_addx:
        # Step 3a: Addx WebRTC path. Browser HAR shows the per-session order is:
        #   selectsingledevice  → getWebrtcTicket → WS attempt
        #   on failure: stoplive → getWebrtcTicket → WS retry (new traceId)
        # We mirror it: do the select once on session start, then let
        # connect_and_stream's internal retry handle stoplive+new-ticket.
        device_region = target.get("region") or auth_data.get("region")
        a4x_user_id = str(auth_data.get("userID", ""))
        serial = target["serialNumber"]

        logger.info(f"Device uses Addx WebRTC — fetching ticket (region={device_region}) ...")
        ticket = await get_addx_ticket(auth_data, device=target, device_region=device_region)
        await select_single_device(ticket)

        logger.info(f"Connecting to Addx WebRTC -> RTSP output: {RTSP_OUTPUT}")
        try:
            await connect_and_stream(
                ticket=ticket,
                rtsp_output=RTSP_OUTPUT,
                a4x_user_id=a4x_user_id,
                serial_number=serial,
            )
        finally:
            # Mirror the browser teardown so the cloud doesn't keep a stale
            # session pinned to our (now-dead) traceId. Best-effort.
            await stop_live(ticket)
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
        import time
        t_start = time.monotonic()
        try:
            await run_once()
            logger.warning("Session ended cleanly — reconnecting")
        except Exception as e:
            logger.error(f"Bridge error: {e}", exc_info=(log_level == "DEBUG"))

        elapsed = time.monotonic() - t_start
        if elapsed < 60:
            # Short session = camera rejected us (second PEER_IN / PEER_OUT) or
            # signaling error. Failed handshakes can last up to ~50s (camera waits
            # before sending PEER_OUT), so threshold is 60s not 30s. With the
            # second-PEER_IN fix, failures now end in ~5s so 60s is generous.
            logger.info("Short session — retrying in 2s ...")
            await asyncio.sleep(2)
            retry_delay = 10  # reset backoff
        else:
            logger.info(f"Waiting {retry_delay}s before retry ...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 120)


if __name__ == "__main__":
    asyncio.run(main())
