"""
Netvue / Birdfy cloud API — reverse-engineered from my.birdfy.com web app JS bundles.

Auth flow (confirmed working as of 2025):
  POST https://localweb.nvts.co/v1/users/login/v2
    headers: x-nvs-ucid: 513774810c, x-nvs-udid: <any uuid>
    body:    {username, password: md5(password), locale: "EN"}
  Response: {userID, userName, token, refreshToken, region, localEndpoint, ...}

  NOTE: DO NOT send Authorization: Bearer header — auth uses x-nvs-* signed headers,
  not Bearer tokens. The token from login is used in the NVS signature chain.

Device list:
  GET {localEndpoint}/v1/devices/v3
    headers: x-nvs-signature chain (see _nvs_headers())
  Response: {devices: [{serialNumber, name, onAddx, addxSn, groupId, region, ...}]}

Stream play (for non-onAddx devices — KVS_WEBRTC or AGORA_WEBRTC path):
  POST {localEndpoint}/devices/{serialNumber}/play
    body: {serialNumber, provider: "KVS_WEBRTC", region?, ...}
  Response: {region, credential{accessKey,secretKey,sessionToken}, channel,
             channelArn, clientId, trickleIce, httpsEndpoint, wssEndpoint, iceServerConfig}

A4x/Addx token (for onAddx=true devices — Addx WebRTC path):
  GET {localEndpoint}/v1/addx/token/v2
    headers: x-nvs-signature chain
    params:  region=<device_region>  (optional a4xRegion, forceUpdate)
  Response ticket: {signalServer, groupId, role, id, traceId, time, sign,
                    iceServer:[{url,username,credential}], signalPingInterval, ...}

WebSocket URL (Addx):
  {ticket.signalServer}/{ticket.groupId}/{ticket.role}/{ticket.id}
  ?traceId={ticket.traceId}&time={ticket.time}&sign={ticket.sign}&name=a4x

SDP offer message (Addx WebSocket):
  {messageType:"SDP_OFFER", recipientClientId, senderClientId, sessionId,
   messagePayload: base64(json({sdp, type:"offer"})),
   resolution:"auto", viewerType:"netvue_web_sdk", mode:"vicoo"}

ICE candidate message (Addx WebSocket):
  {messageType:"ICE_CANDIDATE", recipientClientId, senderClientId, sessionId,
   messagePayload: base64(json(candidate)), mode:"vicoo"}

NVS signature:
  def sign(token, ucid, udid, userid, timestamp):
      s = hmac_sha256_hex("nvs1" + token, ucid)
      s = hmac_sha256_hex(s, udid)
      s = hmac_sha256_hex(s, userid)
      s = hmac_sha256_hex(s, timestamp)
      return hmac_sha256_hex(s, "nvs1_request")
"""
import hashlib
import hmac
import json
import logging
import os
import time
import uuid

import aiohttp

logger = logging.getLogger(__name__)

# App constants (observed in web app bundle)
NVS_UCID = os.getenv("NVS_UCID", "513774810c")
NVS_UDID = os.getenv("NVS_UDID", uuid.uuid4().hex)

# Global auth state — populated by login()
_auth_state: dict = {}

# Base login URL (no region prefix needed for login)
AUTH_BASE = "https://localweb.nvts.co/v1"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _hmac_sha256_hex(key: str, data: str) -> str:
    return hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def _nvs_sign(token: str, ucid: str, udid: str, userid: str, timestamp: str) -> str:
    """NVS signature chain: 5-step HMAC-SHA256."""
    s = _hmac_sha256_hex("nvs1" + token, ucid)
    s = _hmac_sha256_hex(s, udid)
    s = _hmac_sha256_hex(s, userid)
    s = _hmac_sha256_hex(s, timestamp)
    return _hmac_sha256_hex(s, "nvs1_request")


def _nvs_headers(token: str = "", user_id: str = "") -> dict:
    """Build x-nvs-* signed request headers."""
    ts = str(int(time.time() * 1000))
    sig = _nvs_sign(token, NVS_UCID, NVS_UDID, user_id, ts)
    return {
        "x-nvs-ucid": NVS_UCID,
        "x-nvs-udid": NVS_UDID,
        "x-nvs-userid": user_id,
        "x-nvs-time": ts,
        "x-nvs-signature": sig,
        "x-nvs-version": '{"signature":2}',
        "User-Agent": "NeWing/5.0 (web)",
    }


async def login(email: str, password: str) -> dict:
    """
    Authenticate with the Netvue/Birdfy API.

    Returns auth dict with at minimum:
        token        — used in NVS signature chain for subsequent API calls
        userID       — user identifier (string)
        region       — e.g. "us-east-1"
        localEndpoint — region-specific base URL, e.g. "https://us-east-1-localweb.nvts.co"

    Raises RuntimeError on failure.
    """
    global _auth_state

    payload = {
        "username": email,
        "password": _md5(password),
        "locale": "EN",
    }

    headers = _nvs_headers()  # no token/userid for login

    async with aiohttp.ClientSession() as session:
        url = f"{AUTH_BASE}/users/login/v2"
        try:
            logger.info(f"AUTH → {url}")
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
                headers=headers,
            ) as resp:
                text = await resp.text()
                logger.debug(f"  HTTP {resp.status}: {text[:800]}")

                if resp.status not in (200, 201):
                    raise RuntimeError(
                        f"Auth HTTP {resp.status}: {text[:400]}\n"
                        "Check NVS_UCID and NVS_UDID env vars, and that credentials are correct."
                    )

                body = json.loads(text)
                # Response is wrapped: {code:0, data:{token, userID, ...}}
                data = body.get("data") or body
                if not data.get("token"):
                    logger.error(f"Full auth response: {body}")
                    raise RuntimeError(
                        "Auth succeeded but no 'token' field found. "
                        "Check full auth response in DEBUG logs."
                    )

                _auth_state = data
                logger.info(
                    f"AUTH OK — userID={data.get('userID')} "
                    f"region={data.get('region')} "
                    f"localEndpoint={data.get('localEndpoint')}"
                )
                return data

        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Auth request failed: {e}") from e


async def get_devices(auth_data: dict) -> list:
    """
    Fetch device list from the region-specific API.

    Returns list of device dicts. Each device has:
        serialNumber, name, onAddx (bool), addxSn, groupId, region, ...

    For onAddx=True devices: use get_addx_ticket() for WebRTC signaling.
    For onAddx=False devices: use get_stream_play() for KVS/Agora WebRTC.
    """
    token = auth_data.get("token", "")
    user_id = str(auth_data.get("userID", ""))
    local_endpoint = auth_data.get("localEndpoint", "")
    if not local_endpoint:
        # Construct from region
        region = auth_data.get("region", "us-east-1")
        local_endpoint = f"https://{region}-localweb.nvts.co"

    url = f"{local_endpoint}/v1/devices/v3"
    headers = _nvs_headers(token=token, user_id=user_id)

    async with aiohttp.ClientSession() as session:
        logger.info(f"DEVICES → {url}")
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            text = await resp.text()
            logger.debug(f"  HTTP {resp.status}: {text[:600]}")
            if resp.status != 200:
                raise RuntimeError(f"Devices HTTP {resp.status}: {text[:300]}")
            body = json.loads(text)
            devices = body.get("data", {}).get("devices") or body.get("devices") or []
            logger.info(f"Got {len(devices)} devices")
            return devices


async def get_addx_ticket(auth_data: dict, device_region: str | None = None) -> dict:
    """
    Get the Addx WebRTC signaling ticket for onAddx=True devices.

    Calls GET {localEndpoint}/v1/addx/token/v2 with optional region param.

    Returns ticket dict containing:
        signalServer    — WebSocket host (e.g. "wss://signal.example.com")
        groupId         — device group identifier
        role            — viewer role string
        id              — client/viewer ID
        traceId         — trace ID for WebSocket URL
        time            — timestamp for WebSocket URL
        sign            — signature for WebSocket URL
        iceServer       — list of {url, username, credential} TURN/STUN servers
        signalPingInterval — heartbeat interval in seconds (default 2)

    WebSocket URL format:
        {ticket.signalServer}/{ticket.groupId}/{ticket.role}/{ticket.id}
        ?traceId={ticket.traceId}&time={ticket.time}&sign={ticket.sign}&name=a4x
    """
    token = auth_data.get("token", "")
    user_id = str(auth_data.get("userID", ""))
    local_endpoint = auth_data.get("localEndpoint", "")
    if not local_endpoint:
        region = auth_data.get("region", "us-east-1")
        local_endpoint = f"https://{region}-localweb.nvts.co"

    params: dict = {}
    if device_region:
        params["region"] = device_region

    url = f"{local_endpoint}/v1/addx/token/v2"
    headers = _nvs_headers(token=token, user_id=user_id)

    async with aiohttp.ClientSession() as session:
        logger.info(f"ADDX TICKET → {url} params={params}")
        async with session.get(
            url,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            text = await resp.text()
            logger.debug(f"  HTTP {resp.status}: {text[:800]}")
            if resp.status != 200:
                raise RuntimeError(f"Addx ticket HTTP {resp.status}: {text[:300]}")
            body = json.loads(text)
            ticket = body.get("data") or body
            logger.info(
                f"Ticket obtained — signalServer={ticket.get('signalServer')} "
                f"groupId={ticket.get('groupId')} role={ticket.get('role')} "
                f"id={ticket.get('id')}"
            )
            return ticket


async def get_stream_play(auth_data: dict, device: dict, provider: str = "KVS_WEBRTC") -> dict:
    """
    Get WebRTC stream parameters for non-Addx devices (KVS_WEBRTC or AGORA_WEBRTC).

    provider: "KVS_WEBRTC" (default) or "AGORA_WEBRTC"

    For KVS_WEBRTC, returns:
        region, credential{accessKey,secretKey,sessionToken,expiration},
        channel, channelArn, clientId, trickleIce,
        httpsEndpoint, wssEndpoint, iceServerConfig
    """
    token = auth_data.get("token", "")
    user_id = str(auth_data.get("userID", ""))
    local_endpoint = auth_data.get("localEndpoint", "")
    if not local_endpoint:
        region = auth_data.get("region", "us-east-1")
        local_endpoint = f"https://{region}-localweb.nvts.co"

    serial = device["serialNumber"]
    url = f"{local_endpoint}/devices/{serial}/play"
    headers = _nvs_headers(token=token, user_id=user_id)

    payload = dict(device)
    payload["provider"] = provider

    async with aiohttp.ClientSession() as session:
        logger.info(f"STREAM PLAY → {url} provider={provider}")
        async with session.post(
            url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            text = await resp.text()
            logger.debug(f"  HTTP {resp.status}: {text[:800]}")
            if resp.status != 200:
                raise RuntimeError(f"Stream play HTTP {resp.status}: {text[:300]}")
            body = json.loads(text)
            data = body.get("data") or body
            logger.info(f"Stream play OK — keys: {list(data.keys()) if isinstance(data, dict) else data}")
            return data
