"""
Integration smoke-test: login → device list → addx ticket → WebSocket connect.

No aiortc or ffmpeg needed. Requires real credentials:
    BIRDFY_EMAIL=... BIRDFY_PASSWORD=... pytest -m integration

Logs are written at INFO/DEBUG via the root logger so a passing run still shows
the signal-server URL, ice server count, etc. for quick spot-checking.
"""
import asyncio
import json
import logging
import os

import pytest

pytestmark = pytest.mark.integration

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def credentials():
    email = os.environ.get("BIRDFY_EMAIL")
    password = os.environ.get("BIRDFY_PASSWORD")
    if not email or not password:
        pytest.skip("BIRDFY_EMAIL / BIRDFY_PASSWORD not set")
    return email, password


@pytest.mark.asyncio
async def test_login(credentials):
    from birdfy_api import login

    email, password = credentials
    auth = await login(email, password)
    assert auth.get("token"), "login returned no token"
    logger.info("token length: %d", len(auth["token"]))
    logger.info("userID: %s  region: %s", auth.get("userID"), auth.get("region"))


@pytest.mark.asyncio
async def test_device_list(credentials):
    from birdfy_api import get_devices, login

    email, password = credentials
    auth = await login(email, password)
    devices = await get_devices(auth)
    assert devices, "device list is empty"
    for d in devices:
        logger.info(
            "device name=%-30r sn=%-20s onAddx=%s region=%s",
            d.get("name"),
            d.get("serialNumber"),
            d.get("onAddx"),
            d.get("region"),
        )


@pytest.mark.asyncio
async def test_addx_ticket_and_websocket(credentials):
    from birdfy_api import get_addx_ticket, get_devices, login

    email, password = credentials
    auth = await login(email, password)
    devices = await get_devices(auth)
    assert devices, "no devices — cannot continue"

    target = next((d for d in devices if d.get("onAddx")), None)
    if target is None:
        pytest.skip("no onAddx device in account")

    ticket = await get_addx_ticket(auth, device=target, device_region=target.get("region"))
    assert ticket.get("signalServer"), "ticket missing signalServer"
    logger.info("signalServer: %s", ticket.get("signalServer"))
    logger.info(
        "groupId=%s role=%s id=%s iceServers=%d",
        ticket.get("groupId"),
        ticket.get("role"),
        ticket.get("id"),
        len(ticket.get("iceServer") or ticket.get("iceServers") or []),
    )

    await _listen_websocket(ticket, timeout=10)


async def _listen_websocket(ticket: dict, timeout: int = 10):
    """Connect to the signaling WS and log whatever the server sends."""
    import websockets

    from webrtc_client import _ws_header_kwargs

    url = (
        f"{ticket['signalServer']}/{ticket['groupId']}"
        f"/{ticket['role']}/{ticket['id']}"
        f"?traceId={ticket.get('traceId','')}"
        f"&time={ticket.get('time','')}"
        f"&sign={ticket.get('sign','')}"
        f"&name=a4x"
    )
    logger.info("connecting to: %.120s…", url)

    async with websockets.connect(
        url,
        **_ws_header_kwargs({"User-Agent": "Mozilla/5.0 (compatible; birdfy-bridge/1.0)"}),
        ping_interval=None,
        open_timeout=10,
    ) as ws:
        logger.info("WebSocket connected — listening %ds", timeout)
        try:
            async with asyncio.timeout(timeout):
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        logger.info("<< %s", json.dumps(msg)[:400])
                    except Exception:
                        logger.info("<< raw: %.300s", raw)
        except asyncio.TimeoutError:
            logger.info("%ds elapsed — closing cleanly", timeout)
