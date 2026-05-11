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
| `RTSP_OUTPUT`   | No       | Full RTSP push URL. If unset, built from `RTSP_HOST` + `RTSP_PATH`. |
| `RTSP_HOST`     | No       | RTSP server host:port (default: `localhost:8554` — the bundled MediaMTX) |
| `RTSP_PATH`     | No       | RTSP stream path (default: `birdfy`) |
| `LOG_LEVEL`     | No       | `DEBUG` / `INFO` / `WARNING` (default: `INFO`) |
| `LOG_FILE`      | No       | Path for file log (default: `birdfy-bridge.log`; empty = stdout only) |
| `NVS_UCID`      | No       | App client ID (default: `513774810c`) |
| `NVS_UDID`      | No       | Stable device UUID for signing (auto-generated per run if not set) |

## Running with Docker

The container bundles [MediaMTX](https://github.com/bluenviron/mediamtx) as an RTSP server so it works standalone: the bridge publishes to MediaMTX inside the container, and consumers (VLC, Frigate) read the stream from the container's exposed port 8554.

```bash
cp .env.example .env
# edit .env with your BIRDFY_EMAIL, BIRDFY_PASSWORD, DEVICE_ID
docker compose up --build -d
docker compose logs -f
```

Then point a player at `rtsp://<docker-host>:8554/birdfy` (VLC: Media → Open Network Stream).

### Pointing Frigate at the bridge

If Frigate runs on the same Docker network, set its camera input to `rtsp://birdfy-bridge:8554/birdfy`. From another host, use `rtsp://<host-running-birdfy-bridge>:8554/birdfy`.

You can also bypass the bundled MediaMTX entirely and publish straight to Frigate's go2rtc by setting `RTSP_OUTPUT=rtsp://frigate:8554/birdfy` in `.env` — in that case the container's MediaMTX still runs but nothing publishes to it.

## Finding your DEVICE_ID

Set `LOG_LEVEL=DEBUG` and look for the "Device found" log lines at startup. The bridge prints all available device serial numbers if your `DEVICE_ID` isn't found.

## What's broken / not done

- **KVS path**: `onAddx: false` devices (some outdoor cameras) need a separate boto3-based implementation — currently raises `NotImplementedError`
- **Initial keyframe latency**: After the data channel opens, the camera takes ~15s to send a decodable keyframe. Sending an RTCP PLI immediately after `startLive` would shorten this
- **Token refresh**: The `token` field has an expiry. Production use needs the `/auth/refreshtoken` endpoint
- **Audio**: Audio track is received but not currently muxed into the RTSP output (video-only)

## Dependencies

### Python (requirements.txt)
- `aiortc` — WebRTC library (handles SDP, ICE, H264 decode)
- `aiohttp` — async HTTP for API calls
- `websockets` — WebSocket client for signaling
- `av`, `numpy` — video frame handling

### System (Docker image installs these)
- `ffmpeg` — re-encode H264 + push RTSP
- `mediamtx` — bundled RTSP server (latest release fetched at build time)
- `s6-overlay` — process supervisor for running mediamtx + bridge together

## Debugging

Set `LOG_LEVEL=DEBUG` to see:
- Full auth response JSON
- Raw WebSocket messages
- ICE candidate details
- ffmpeg stderr at `/tmp/ffmpeg_birdfy.log`
