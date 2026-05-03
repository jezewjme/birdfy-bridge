# birdfy-bridge

Converts a Birdfy/Netvue camera stream to RTSP for use by Frigate, go2rtc, or any RTSP consumer.

## How it works

The Birdfy/Netvue cloud API uses a custom auth scheme reverse-engineered from the `my.birdfy.com` web app JavaScript bundles. There are two WebRTC paths depending on device type:

### Addx WebRTC path (Birdfy Feeder Bamboo, Feeder, Cam — `onAddx: true`)

1. **Auth**: `POST https://localweb.nvts.co/v1/users/login/v2`
   - Body: `{username, password: md5(password), locale: "EN"}`
   - Headers: `x-nvs-ucid: 513774810c`, `x-nvs-udid: <uuid>` (no Bearer token)
   - Response: `{token, userID, region, localEndpoint, ...}`
   - `localEndpoint` = `https://{region}-localweb.nvts.co`

2. **Device list**: `GET {localEndpoint}/v1/devices/v3`
   - Headers: NVS signature chain (HMAC-SHA256, see `birdfy_api.py::_nvs_sign`)
   - Response: `{devices: [{serialNumber, name, onAddx, addxSn, groupId, region, ...}]}`

3. **Addx ticket**: `GET {localEndpoint}/v1/addx/token/v2?region={device_region}`
   - Headers: NVS signature chain
   - Response ticket: `{signalServer, groupId, role, id, traceId, time, sign, iceServer:[...], signalPingInterval}`

4. **WebSocket URL** (from ticket):
   ```
   {ticket.signalServer}/{ticket.groupId}/{ticket.role}/{ticket.id}
   ?traceId={ticket.traceId}&time={ticket.time}&sign={ticket.sign}&name=a4x
   ```

5. **WebRTC negotiation** over WebSocket:
   - We (viewer) send `SDP_OFFER` — camera (master) sends `SDP_ANSWER`
   - Message format: `{messageType, recipientClientId, senderClientId, sessionId, messagePayload: base64(json({sdp, type})), viewerType: "netvue_web_sdk", mode: "vicoo"}`
   - ICE candidates use same format with `messagePayload: base64(json(candidate))`
   - Heartbeat: re-send last cached ICE candidate every `ticket.signalPingInterval` seconds

6. **Video** received as H264 via aiortc, decoded to raw YUV420p frames, piped to ffmpeg for re-encoding and RTSP push.

### KVS WebRTC path (`onAddx: false` — NOT YET IMPLEMENTED)

Some newer/outdoor Netvue cameras use AWS Kinesis Video Streams WebRTC:
- `POST {localEndpoint}/devices/{serialNumber}/play` with `provider: "KVS_WEBRTC"`
- Returns AWS credentials + channel ARN
- Requires `boto3` or the KVS WebRTC JavaScript SDK

## NVS Signature Algorithm

```python
import hmac, hashlib

def nvs_sign(token, ucid, udid, userid, timestamp):
    s = hmac.new(("nvs1" + token).encode(), ucid.encode(), hashlib.sha256).hexdigest()
    s = hmac.new(s.encode(), udid.encode(), hashlib.sha256).hexdigest()
    s = hmac.new(s.encode(), userid.encode(), hashlib.sha256).hexdigest()
    s = hmac.new(s.encode(), timestamp.encode(), hashlib.sha256).hexdigest()
    return hmac.new(s.encode(), b"nvs1_request", hashlib.sha256).hexdigest()
```

Required headers for all authenticated API calls:
```
x-nvs-ucid:      513774810c
x-nvs-udid:      <any stable UUID>
x-nvs-userid:    <userID from login>
x-nvs-time:      <unix timestamp milliseconds as string>
x-nvs-signature: <nvs_sign(token, ucid, udid, userid, time)>
x-nvs-version:   {"signature":2}
```

## Configuration

| Env var         | Required | Description |
|----------------|----------|-------------|
| `BIRDFY_EMAIL`  | Yes      | Netvue/Birdfy account email |
| `BIRDFY_PASSWORD` | Yes    | Account password (plain text, MD5'd internally) |
| `DEVICE_ID`     | Yes      | Camera serial number (e.g. `5372540233101051`) — shown on device list startup log |
| `RTSP_OUTPUT`   | No       | RTSP push URL (default: `rtsp://frigate:8554/birdfy`) |
| `LOG_LEVEL`     | No       | `DEBUG` / `INFO` / `WARNING` (default: `INFO`) |
| `NVS_UCID`      | No       | App client ID (default: `513774810c`) |
| `NVS_UDID`      | No       | Stable device UUID for signing (auto-generated per run if not set) |

## Running with Docker Compose

```yaml
services:
  birdfy-bridge:
    build: ./birdfy-bridge
    restart: unless-stopped
    environment:
      BIRDFY_EMAIL: your@email.com
      BIRDFY_PASSWORD: yourpassword
      DEVICE_ID: "5372540233101051"
      RTSP_OUTPUT: rtsp://go2rtc:8554/birdfy
      LOG_LEVEL: INFO
```

## Finding your DEVICE_ID

Set `LOG_LEVEL=DEBUG` and look for the "Device found" log lines at startup. The bridge prints all available device serial numbers if your `DEVICE_ID` isn't found.

## What's broken / not done

- **KVS path**: `onAddx: false` devices (some outdoor cameras) need a separate boto3-based implementation — currently raises `NotImplementedError`
- **WebSocket message format**: The `recipientClientId` for Addx is unclear — may need to be the `addxSn` field, empty string, or something from the ticket. This is the most likely cause of connection issues during initial testing
- **SDP offer timing**: We may need to wait for a "peer in" message from the server before sending the SDP offer. The Addx client JS sends the offer only after `onRemotePeerIn` fires
- **Data channel**: The `startLive` data channel message is sent on `onDataChannelOpen` — the device may require this before sending video
- **Token refresh**: The `token` field has an expiry. Production use needs the `/auth/refreshtoken` endpoint

## Dependencies

- `aiortc` — WebRTC library (handles SDP, ICE, H264 decode)
- `aiohttp` — async HTTP for API calls
- `websockets` — WebSocket client for signaling
- `av`, `numpy` — video frame handling
- `ffmpeg` (system binary) — re-encode and push RTSP

## Debugging

Set `LOG_LEVEL=DEBUG` to see:
- Full auth response JSON
- Raw WebSocket messages
- ICE candidate details
- ffmpeg stderr at `/tmp/ffmpeg_birdfy.log`
