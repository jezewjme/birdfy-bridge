"""
Quick API test — no aiortc/ffmpeg needed.
Tests: login → device list → addx ticket → websocket connect

Run:
    python test_api.py
"""
import asyncio
import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test")

EMAIL    = os.getenv("BIRDFY_EMAIL",    "jjezewski@gmail.com")
PASSWORD = os.getenv("BIRDFY_PASSWORD", "iPoG8hJiAdpwbTbE")


async def main():
    from birdfy_api import get_addx_ticket, get_devices, login

    # ── Step 1: Auth ──────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("STEP 1: LOGIN")
    logger.info("=" * 60)
    auth = await login(EMAIL, PASSWORD)
    logger.info(f"Auth keys: {list(auth.keys())}")
    logger.info(f"token (first 20): {str(auth.get('token',''))[:20]}...")
    logger.info(f"userID:           {auth.get('userID')}")
    logger.info(f"region:           {auth.get('region')}")
    logger.info(f"localEndpoint:    {auth.get('localEndpoint')}")
    print()

    # ── Step 2: Device list ───────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("STEP 2: DEVICE LIST")
    logger.info("=" * 60)
    devices = await get_devices(auth)
    for d in devices:
        logger.info(
            f"  Device: name={d.get('name')!r:30s} "
            f"sn={d.get('serialNumber'):20s} "
            f"onAddx={d.get('onAddx')} "
            f"region={d.get('region')}"
        )
    print()
    if not devices:
        logger.error("No devices found — stopping here")
        return

    target = devices[0]
    device_name = target.get("name") or target.get("deviceName") or target.get("nickName") or "unknown"
    logger.info(f"Using first device: {device_name!r} sn={target.get('serialNumber')}")
    logger.info(f"Full device JSON:\n{json.dumps(target, indent=2)[:3000]}")
    print()

    # ── Step 3: Addx ticket (if onAddx) ──────────────────────────────────
    if target.get("onAddx"):
        logger.info("=" * 60)
        logger.info("STEP 3: ADDX TICKET")
        logger.info("=" * 60)
        ticket = await get_addx_ticket(auth, device=target, device_region=target.get("region"))
        logger.info(f"Ticket keys: {list(ticket.keys())}")
        logger.info(f"signalServer:      {ticket.get('signalServer')}")
        logger.info(f"groupId:           {ticket.get('groupId')}")
        logger.info(f"role:              {ticket.get('role')}")
        logger.info(f"id:                {ticket.get('id')}")
        logger.info(f"signalPingInterval:{ticket.get('signalPingInterval')}")
        logger.info(f"iceServer count:   {len(ticket.get('iceServer') or ticket.get('iceServers') or [])}")
        print()

        # ── Step 4: WebSocket connect (no WebRTC, just see what server sends) ──
        logger.info("=" * 60)
        logger.info("STEP 4: WEBSOCKET CONNECT (listen only, 10s)")
        logger.info("=" * 60)
        await test_websocket(ticket)
    else:
        logger.warning(
            f"Device onAddx=False — uses AWS KVS WebRTC, not yet implemented.\n"
            f"Device details: {json.dumps({k: target.get(k) for k in ['serialNumber','name','onAddx','region']}, indent=2)}"
        )


async def test_websocket(ticket: dict):
    """Connect to the signaling WS and print whatever the server sends for 10 seconds."""
    import websockets

    signal_server = ticket["signalServer"]
    group_id      = ticket["groupId"]
    role          = ticket["role"]
    client_id     = ticket["id"]
    trace_id      = ticket.get("traceId", "")
    ts            = ticket.get("time", "")
    sign          = ticket.get("sign", "")

    url = (
        f"{signal_server}/{group_id}/{role}/{client_id}"
        f"?traceId={trace_id}&time={ts}&sign={sign}&name=a4x"
    )
    logger.info(f"Connecting to: {url[:120]}...")

    try:
        async with websockets.connect(
            url,
            additional_headers={"User-Agent": "Mozilla/5.0 (compatible; birdfy-bridge/1.0)"},
            ping_interval=None,
            open_timeout=10,
        ) as ws:
            logger.info("WebSocket connected! Listening for 10 seconds...")
            try:
                async with asyncio.timeout(10):
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            logger.info(f"<< WS message: {json.dumps(msg, indent=2)[:500]}")
                        except Exception:
                            logger.info(f"<< WS raw: {raw[:300]}")
            except asyncio.TimeoutError:
                logger.info("10s elapsed — closing cleanly")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)


if __name__ == "__main__":
    # Install websockets if needed for step 4
    try:
        import websockets  # noqa
    except ImportError:
        logger.warning("websockets not installed — skipping WS test. Run: pip install websockets")

    asyncio.run(main())
