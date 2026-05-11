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
import pathlib
import time
import uuid

import aiohttp

logger = logging.getLogger(__name__)

# App constants (observed in web app bundle)
NVS_UCID = os.getenv("NVS_UCID", "513774810c")

# Persist UDID so the Netvue backend sees the same "device" on every restart
# and doesn't spam "new device connected" notifications.
_UDID_FILE = pathlib.Path.home() / ".birdfy_nvs_udid"
if os.getenv("NVS_UDID"):
    NVS_UDID = os.getenv("NVS_UDID")
elif _UDID_FILE.exists():
    NVS_UDID = _UDID_FILE.read_text().strip()
else:
    NVS_UDID = uuid.uuid4().hex
    try:
        _UDID_FILE.write_text(NVS_UDID)
        logger.info(f"Generated new NVS_UDID and saved to {_UDID_FILE}")
    except Exception:
        pass

# Global auth state — populated by login()
_auth_state: dict = {}

# Base login URL (no region prefix needed for login)
AUTH_BASE = "https://localweb.nvts.co/v1"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


# Fields that should never appear in plaintext in logs, even at DEBUG level.
# Cloud responses for auth + ticketing contain tokens, signed URLs, AWS
# credentials, and ICE TURN creds — leaking any of these gives an attacker
# the same access as the bridge.
_SENSITIVE_FIELDS = frozenset({
    "token", "accessToken", "refreshToken", "sessionToken",
    "sign", "signature", "secret", "secretKey", "accessKey",
    "password", "credential", "credentials",
})


def _redact_response(text: str, *, limit: int = 600) -> str:
    """Best-effort scrub of secret-bearing fields from a JSON response body.

    Used when logging HTTP responses at DEBUG. Falls back to logging only the
    keys + status if the body isn't JSON.
    """
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        return f"<non-JSON, {len(text)}B>"

    def _scrub(obj):
        if isinstance(obj, dict):
            return {
                k: ("***REDACTED***" if k in _SENSITIVE_FIELDS else _scrub(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_scrub(v) for v in obj]
        return obj

    scrubbed = json.dumps(_scrub(body), separators=(",", ":"))
    if len(scrubbed) > limit:
        return scrubbed[:limit] + "...<truncated>"
    return scrubbed


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
            logger.info(f"AUTH -> {url}")
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
                headers=headers,
            ) as resp:
                text = await resp.text()
                logger.debug(f"  HTTP {resp.status}: {_redact_response(text)}")

                if resp.status not in (200, 201):
                    raise RuntimeError(
                        f"Auth HTTP {resp.status}: {_redact_response(text, limit=400)}\n"
                        "Check NVS_UCID and NVS_UDID env vars, and that credentials are correct."
                    )

                body = json.loads(text)
                # Response is wrapped: {code:0, data:{token, userID, ...}}
                data = body.get("data") or body
                if not data.get("token"):
                    logger.error(
                        f"Auth response missing 'token'. Keys present: "
                        f"{list(body.keys()) if isinstance(body, dict) else type(body).__name__}"
                    )
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
        logger.info(f"DEVICES -> {url}")
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            text = await resp.text()
            logger.debug(f"  HTTP {resp.status}: {_redact_response(text)}")
            if resp.status != 200:
                raise RuntimeError(f"Devices HTTP {resp.status}: {_redact_response(text, limit=300)}")
            body = json.loads(text)
            devices = body.get("data", {}).get("devices") or body.get("devices") or []
            logger.info(f"Got {len(devices)} devices")
            return devices


_APP_OBJECT = {
    "bundle": "com.netviewtech.mynetvue",
    "channelId": 1000,
    "appBuild": "online-build",
    "appName": "Netvue",
    "tenantId": "netvue",
    "countlyId": "",
    "version": 99999,
    "appType": "iOS",
}


async def get_addx_ticket(
    auth_data: dict,
    device: dict | None = None,
    device_region: str | None = None,
    *,
    addx_state: dict | None = None,
) -> dict:
    """
    Get the Addx WebRTC signaling ticket for onAddx=True devices.

    Two-step process (confirmed from my.birdfy.com JS bundles):
      1. GET https://api2.nvts.co/addx/token/v2
            Authorization: Bearer <login token>
         -> returns {token, endpoint, language, countryNo, ...}

      2. POST {endpoint}device/getWebrtcTicket
            Authorization: <addx token from step 1>
            body: {requestId, serialNumber: addxSn, app: {...}, ...}
         -> returns {signalServer, groupId, role, id, traceId, time, sign, iceServer, ...}

    WebSocket URL: {signalServer}/{groupId}/{role}/{id}?traceId=...&time=...&sign=...&name=a4x

    addx_state (in/out): if provided, the addx token + endpoint + language are stored
    here on first call and reused by callers who later need them (e.g. select/stop).
    """
    login_token = auth_data.get("token", "")
    addx_sn = (device or {}).get("addxSn") or (device or {}).get("serialNumber", "")
    region = device_region or (device or {}).get("region") or auth_data.get("region", "us-east-1")

    async with aiohttp.ClientSession() as session:
        # ── Step 1: Get addx token from api2.nvts.co ──────────────────────
        addx_token_url = "https://api2.nvts.co/addx/token/v2"
        params = {"region": region}

        # Try different auth header formats until one works
        nvs_hdrs = _nvs_headers(token=login_token, user_id=str(auth_data.get("userID", "")))
        header_candidates = [
            # NVS signature (same as localweb calls)
            nvs_hdrs,
            # Bearer only
            {"Authorization": f"Bearer {login_token}", "x-nvs-version": '{"signature":2}',
             "User-Agent": "NeWing/5.0 (web)"},
            # NVS signature + Bearer combined
            {**nvs_hdrs, "Authorization": f"Bearer {login_token}"},
            # Plain token header
            {"Authorization": login_token, "x-nvs-version": '{"signature":2}',
             "User-Agent": "NeWing/5.0 (web)"},
        ]

        text = ""
        for addx_headers in header_candidates:
            logger.info(f"ADDX TOKEN -> {addx_token_url} auth_style={list(addx_headers.keys())[:3]}")
            async with session.get(
                addx_token_url,
                params=params,
                headers=addx_headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                text = await resp.text()
                logger.debug(f"  HTTP {resp.status}: {_redact_response(text)}")
                if resp.status == 200:
                    break
                logger.warning(f"  HTTP {resp.status} with this auth style, trying next...")
        else:
            raise RuntimeError(
                f"Addx token failed with all auth styles. Last response: {_redact_response(text, limit=300)}"
            )

        body = json.loads(text)
        addx_data = body.get("data") or body
        addx_token = addx_data.get("token") or addx_data.get("accessToken")
        addx_endpoint = addx_data.get("endpoint", "").rstrip("/") + "/"
        language = addx_data.get("language", "en")
        country_no = addx_data.get("countryNo", "US")
        if not addx_token or not addx_endpoint:
            logger.error(f"Addx token response: {_redact_response(text)}")
            raise RuntimeError(
                "Addx token response missing 'token' or 'endpoint' fields. "
                "Check DEBUG logs for full response."
            )
        logger.info(f"Addx token OK -- endpoint={addx_endpoint} language={language}")

        # ── Step 2: Get WebRTC ticket from device endpoint ─────────────────
        ticket_url = f"{addx_endpoint}device/getWebrtcTicket"
        ticket_headers = {
            "Authorization": addx_token,
            "Content-Type": "application/json",
            "User-Agent": "NeWing/5.0 (web)",
        }
        ticket_body = {
            "requestId": uuid.uuid4().hex,
            "language": language,
            "countryNo": country_no.upper(),
            "app": _APP_OBJECT,
            "serialNumber": addx_sn,
            "verifyDormancyStatus": True,
        }

        logger.info(f"WEBRTC TICKET -> {ticket_url} serialNumber={addx_sn}")
        async with session.post(
            ticket_url,
            json=ticket_body,
            headers=ticket_headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            text = await resp.text()
            logger.debug(f"  HTTP {resp.status}: {_redact_response(text)}")
            if resp.status != 200:
                raise RuntimeError(f"WebRTC ticket HTTP {resp.status}: {_redact_response(text, limit=300)}")
            body = json.loads(text)
            ticket = body.get("data") or body
            result = ticket.get("result", ticket.get("code", 0))
            if result != 0:
                raise RuntimeError(
                    f"WebRTC ticket error result={result}: {_redact_response(text, limit=300)}"
                )
            logger.info(
                f"Ticket OK -- signalServer={ticket.get('signalServer')} "
                f"groupId={ticket.get('groupId')} role={ticket.get('role')} "
                f"id={ticket.get('id')}"
            )

        # Stash addx state on the ticket so callers can issue subsequent
        # selectsingledevice / stoplive / new-ticket calls without re-deriving
        # endpoint/token/serial.
        ticket["_addx_endpoint"] = addx_endpoint
        ticket["_addx_token"] = addx_token
        ticket["_addx_sn"] = addx_sn
        ticket["_language"] = language
        ticket["_country_no"] = country_no
        ticket["_region"] = region

        if addx_state is not None:
            for key in ("_addx_endpoint", "_addx_token", "_addx_sn", "_language", "_country_no", "_region"):
                addx_state[key] = ticket[key]

        return ticket


async def select_single_device(ticket: dict) -> bool:
    """
    POST {endpoint}device/selectsingledevice — first call the browser makes
    after addx-token/before getWebrtcTicket. Appears to "wake" the camera's
    cloud subscription so the subsequent WebRTC handshake can complete.

    Pulls endpoint/token/serial/language from the ticket dict (or any compatible
    state dict with the _addx_* keys).

    Returns True if cloud returned 200, False otherwise (best-effort: never raises).
    """
    endpoint = ticket["_addx_endpoint"]
    addx_token = ticket["_addx_token"]
    addx_sn = ticket["_addx_sn"]
    language = ticket["_language"]
    country_no = (ticket.get("_country_no") or "").upper()

    body = {
        "serialNumber": addx_sn,
        "app": _APP_OBJECT,
        "language": language,
        "countryNo": country_no,
        "requestId": str(uuid.uuid4()),
    }
    region = ticket.get("_region") or ""
    headers = {
        "Authorization": addx_token,
        "Content-Type": "application/json",
        "User-Agent": "NeWing/5.0 (web)",
    }
    if region:
        headers["x-nvs-a4x-region"] = region

    url = f"{endpoint}device/selectsingledevice"
    logger.info(f"SELECT DEVICE -> {url} sn={addx_sn}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                text = await resp.text()
                logger.debug(f"  HTTP {resp.status}: {_redact_response(text, limit=400)}")
                if resp.status != 200:
                    logger.warning(
                        f"selectsingledevice HTTP {resp.status}: {_redact_response(text, limit=200)}"
                    )
                    return False
                logger.info("selectsingledevice OK")
                return True
    except Exception as e:
        logger.warning(f"selectsingledevice call failed: {e}")
        return False


async def stop_live(ticket: dict) -> bool:
    """
    POST {endpoint}device/stoplive — the browser calls this between a failed
    WebRTC attempt and a fresh getWebrtcTicket. Tells the cloud to drop any
    half-open session so the camera will accept the next handshake.
    Best-effort; never raises.
    """
    endpoint = ticket["_addx_endpoint"]
    addx_token = ticket["_addx_token"]
    addx_sn = ticket["_addx_sn"]
    language = ticket["_language"]
    country_no = (ticket.get("_country_no") or "").upper()

    body = {
        "serialNumber": addx_sn,
        "app": _APP_OBJECT,
        "language": language,
        "countryNo": country_no,
        "requestId": str(uuid.uuid4()),
    }
    headers = {
        "Authorization": addx_token,
        "Content-Type": "application/json",
        "User-Agent": "NeWing/5.0 (web)",
    }
    url = f"{endpoint}device/stoplive"
    logger.info(f"STOP LIVE -> {url} sn={addx_sn}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                text = await resp.text()
                logger.debug(f"  HTTP {resp.status}: {_redact_response(text, limit=400)}")
                if resp.status != 200:
                    logger.warning(
                        f"stoplive HTTP {resp.status}: {_redact_response(text, limit=200)}"
                    )
                    return False
                return True
    except Exception as e:
        logger.warning(f"stoplive call failed: {e}")
        return False


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
        logger.info(f"STREAM PLAY -> {url} provider={provider}")
        async with session.post(
            url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            text = await resp.text()
            logger.debug(f"  HTTP {resp.status}: {_redact_response(text)}")
            if resp.status != 200:
                raise RuntimeError(
                    f"Stream play HTTP {resp.status}: {_redact_response(text, limit=300)}"
                )
            body = json.loads(text)
            data = body.get("data") or body
            logger.info(f"Stream play OK — keys: {list(data.keys()) if isinstance(data, dict) else data}")
            return data
