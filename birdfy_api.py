"""
Netvue / Birdfy cloud API authentication.

The exact endpoints and field names are reverse-engineered — if auth fails,
check the DEBUG logs for the raw API responses and update FIELD MAPPING below.
"""
import hashlib
import json
import logging
import os
import uuid

import aiohttp

logger = logging.getLogger(__name__)

# Override via env if your account is on a different regional server
NETVUE_API = os.getenv("NETVUE_API", "https://user-na.netvue.com")

# Candidate auth endpoints — tried in order until one returns code==0
AUTH_ENDPOINTS = [
    f"{NETVUE_API}/apiv1/user/login",
    f"{NETVUE_API}/apiv1/user/token",
    "https://apim.netvue.com/aim/api/user/login",
    "https://openapi.netvue.com/open/token",
]

# Candidate stream-info endpoints — tried in order
STREAM_ENDPOINTS = [
    "/apiv1/device/live/{device_id}",
    "/apiv1/streaming/live?deviceId={device_id}",
    "/apiv1/device/webrtc/{device_id}",
]


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


async def login(email: str, password: str) -> dict:
    """
    Authenticate with the Netvue/Birdfy API.

    Returns a dict that should contain at minimum:
        accessToken  — JWT used for API calls
        userId       — user identifier

    On failure raises RuntimeError with instructions for debugging.
    """
    device_id = uuid.uuid4().hex

    payloads = [
        # Most common pattern: MD5 password
        {"account": email, "password": _md5(password), "deviceId": device_id,
         "locale": "EN", "appType": "web", "clientType": "web"},
        # Fallback: plain password
        {"account": email, "password": password, "deviceId": device_id,
         "locale": "EN", "appType": "web"},
        # Alt field names
        {"username": email, "password": _md5(password), "grant_type": "password"},
    ]

    async with aiohttp.ClientSession() as session:
        for url in AUTH_ENDPOINTS:
            for payload in payloads:
                try:
                    logger.info(f"AUTH → {url}")
                    async with session.post(
                        url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=15),
                        headers={"User-Agent": "NeWing/5.0 (web)"},
                    ) as resp:
                        text = await resp.text()
                        logger.debug(f"  HTTP {resp.status}: {text[:600]}")

                        if resp.status not in (200, 201):
                            continue

                        body = json.loads(text)
                        code = body.get("code") or body.get("errCode") or body.get("status")
                        success = (code == 0 or code == "0" or code == "200"
                                   or body.get("success") is True
                                   or "accessToken" in body
                                   or "accessToken" in body.get("data", {}))
                        if success:
                            logger.info(f"AUTH OK via {url}")
                            return body.get("data") or body
                except Exception as e:
                    logger.warning(f"  {url} → {e}")

    raise RuntimeError(
        "All Netvue auth endpoints failed.\n"
        "Set log level to DEBUG (LOG_LEVEL=DEBUG env var) and check the raw responses above.\n"
        "Look for the endpoint that returns HTTP 200 with a JSON body containing 'accessToken'.\n"
        "Then set NETVUE_API env var to the matching base URL."
    )


async def get_stream_info(device_id: str, access_token: str) -> dict:
    """
    Fetch WebRTC signaling parameters for the device.

    Returns a dict — field names vary by API version.
    FIELD MAPPING (update if needed):
        signalServerId / serverId / server  → server_id for WebSocket URL
        clientId / viewerId / viewer_id     → our viewer client ID
        signalToken / liveToken / token     → WebRTC access token
        turnUrls / iceServers               → TURN/STUN servers (if present)
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "NeWing/5.0 (web)",
    }

    async with aiohttp.ClientSession() as session:
        for path_template in STREAM_ENDPOINTS:
            url = NETVUE_API + path_template.format(device_id=device_id)
            try:
                logger.info(f"STREAM INFO → {url}")
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    text = await resp.text()
                    logger.debug(f"  HTTP {resp.status}: {text[:600]}")

                    if resp.status not in (200, 201):
                        continue

                    body = json.loads(text)
                    code = body.get("code") or body.get("errCode") or body.get("status")
                    success = (code == 0 or code == "0"
                               or body.get("success") is True
                               or "data" in body)
                    if success:
                        logger.info(f"STREAM INFO OK via {url}")
                        data = body.get("data") or body
                        logger.info(f"Stream data keys: {list(data.keys()) if isinstance(data, dict) else data}")
                        return data
            except Exception as e:
                logger.warning(f"  {url} → {e}")

    raise RuntimeError(
        "Failed to get stream info from any endpoint.\n"
        "If the auth step logged a response that already contains signaling info\n"
        "(e.g. serverUrl, signalUrl, wsUrl), set SKIP_STREAM_INFO=1 and fill in\n"
        "SERVER_ID / CLIENT_ID env vars manually."
    )
