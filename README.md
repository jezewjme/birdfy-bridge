# birdfy-bridge

An interoperability tool that converts a Birdfy / Netvue camera's WebRTC stream into RTSP so it can be consumed by Frigate, go2rtc, VLC, Home Assistant, or anything else that speaks RTSP.

> **Unofficial, unaffiliated client.** Not endorsed by, sponsored by, or affiliated with Netvue Inc., Birdfy, Vicohome, or Addx. "Birdfy", "Netvue", "Vicohome", and "Addx" are trademarks of their respective owners and are used here only nominatively to identify the cameras this tool interoperates with.
>
> This project is for use with cameras **you own**. Using it may violate Netvue's Terms of Service and could result in your Birdfy account being suspended or terminated — use a **dedicated account**, not your primary one. The project ships with no warranty (see [LICENSE](LICENSE)).

## Supported cameras

- **Birdfy Feeder Bamboo** (confirmed)
- Other Addx-based Birdfy / Netvue cameras (`onAddx: true` in the device list) should work; please open an issue if you try one.
- Newer KVS-based devices (`onAddx: false`) are not yet supported — see [What's not done](#whats-not-done).

## Quick start (Docker)

```bash
git clone https://github.com/<owner>/birdfy-bridge.git
cd birdfy-bridge
cp .env.example .env
# edit .env with your BIRDFY_EMAIL, BIRDFY_PASSWORD, DEVICE_ID
docker compose up --build -d
docker compose logs -f
```

Then point any RTSP client at `rtsp://<docker-host>:8554/birdfy` (VLC: Media → Open Network Stream).

The container bundles [MediaMTX](https://github.com/bluenviron/mediamtx) as an in-container RTSP server, so it works standalone — no external RTSP infra required.

### Finding your DEVICE_ID

Run the bridge once with any placeholder DEVICE_ID. The error message lists every device on your account with its serial number:

```
RuntimeError: Device '1234567890123456' not found. Available devices:
  ['5372540233101051 / addxSn=AB12CD34 (Bamboo Feeder)', ...]
```

Copy the matching `serialNumber` into `.env`.

### Pointing Frigate at the bridge

If Frigate runs on the same Docker network: `rtsp://birdfy-bridge:8554/birdfy`.

From another host: `rtsp://<host-running-birdfy-bridge>:8554/birdfy`.

You can bypass the bundled MediaMTX entirely and publish straight to Frigate's go2rtc by setting `RTSP_OUTPUT=rtsp://frigate:8554/birdfy` in `.env`.

## Configuration

| Env var          | Required | Description |
|------------------|----------|-------------|
| `BIRDFY_EMAIL`   | Yes      | Netvue / Birdfy account email |
| `BIRDFY_PASSWORD`| Yes      | Account password (plain text, MD5'd internally) |
| `DEVICE_ID`      | Yes      | Camera serial number (see [Finding your DEVICE_ID](#finding-your-device_id)) |
| `RTSP_OUTPUT`    | No       | Full RTSP push URL. If unset, built from `RTSP_HOST` + `RTSP_PATH`. |
| `RTSP_HOST`      | No       | RTSP server host:port (default: `localhost:8554` — the bundled MediaMTX) |
| `RTSP_PATH`      | No       | RTSP stream path (default: `birdfy`) |
| `LOG_LEVEL`      | No       | `DEBUG` / `INFO` / `WARNING` (default: `INFO`) |
| `LOG_FILE`       | No       | Path for file log (default: `birdfy-bridge.log`; empty = stdout only) |
| `NVS_UCID`       | No       | App client ID (default: `513774810c`) |
| `NVS_UDID`       | No       | Stable device UUID for signing (auto-generated and persisted per-host if unset) |

## Security warnings

- **`.env` contains your full Birdfy account password.** Anyone with that file can log into your account and reach every camera, doorbell, or other Netvue device on it. Keep it out of backups, repos, and shared drives. Use a dedicated Birdfy account for the bridge if at all possible.
- **The bundled MediaMTX has no authentication.** This is fine on a trusted LAN. **Do not expose port 8554 to the public internet** — anyone who can reach it can pull your stream. If you must, configure `publishUser` / `readUser` in `docker/mediamtx.yml` and put the container behind a reverse proxy.
- **DEBUG logs may still contain non-secret identifying data** (device serials, region info, ICE server URLs). Bearer tokens, signed URLs, refresh tokens, and AWS credentials are redacted, but treat DEBUG logs as semi-sensitive when sharing them in bug reports.
- **Netvue's API is reverse-engineered, not contracted.** It can change at any time. The bridge fails loudly when it does; please open an issue if your account suddenly stops authenticating or device-list returns 4xx.

## What's not done

- **KVS WebRTC path**: `onAddx: false` devices (some newer outdoor cameras) use AWS Kinesis Video Streams. Currently raises `NotImplementedError`. Contributions welcome.
- **Initial keyframe latency**: After the data channel opens, the camera takes ~15s to send a decodable keyframe. Sending an RTCP PLI immediately after `startLive` would shorten this.
- **Token refresh**: The login token has an expiry. The bridge currently re-authenticates on disconnect; the `/auth/refreshtoken` endpoint isn't used.
- **Audio**: Audio track is received but not muxed into the RTSP output (video-only).

## How it works

The Birdfy / Netvue cloud API uses a custom auth scheme reverse-engineered from the `my.birdfy.com` web app JavaScript bundles. There are two WebRTC paths depending on device type.

### Addx WebRTC path (`onAddx: true`)

1. **Auth**: `POST https://localweb.nvts.co/v1/users/login/v2`
   - Body: `{username, password: md5(password), locale: "EN"}`
   - Headers: `x-nvs-ucid: 513774810c`, `x-nvs-udid: <uuid>` (no Bearer token)
   - Response: `{token, userID, region, localEndpoint, ...}`

2. **Device list**: `GET {localEndpoint}/v1/devices/v3`
   - Headers: NVS signature chain (HMAC-SHA256, see [`birdfy_api.py::_nvs_sign`](birdfy_api.py))
   - Response: `{devices: [{serialNumber, name, onAddx, addxSn, groupId, region, ...}]}`

3. **Addx ticket**: `GET https://api2.nvts.co/addx/token/v2` → `POST {endpoint}device/getWebrtcTicket`
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

### KVS WebRTC path (`onAddx: false` — not yet implemented)

Some newer / outdoor Netvue cameras use AWS Kinesis Video Streams WebRTC:
- `POST {localEndpoint}/devices/{serialNumber}/play` with `provider: "KVS_WEBRTC"`
- Returns AWS credentials + channel ARN
- Requires `boto3` or the KVS WebRTC JavaScript SDK

### NVS Signature algorithm

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

## SDP / ICE / DTLS quirks

The camera's WebRTC stack rejects several things aiortc emits by default. See [`_sdp_patches.py`](_sdp_patches.py) and [`_aioice_patches.py`](_aioice_patches.py) for the rewrites and runtime patches — each one is necessary and load-bearing; do not "clean them up" without consulting the pcap evidence referenced in the comments.

## Development

```bash
python -m venv .venv && . .venv/bin/activate   # Linux/macOS
# or: .venv\Scripts\Activate.ps1                # Windows PowerShell
pip install -r requirements.txt -r requirements-dev.txt
pytest                   # unit tests, no network
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for more.

## Dependencies

### Python (`requirements.txt`)
- `aiortc` — WebRTC library (handles SDP, ICE, H264 decode)
- `aiohttp` — async HTTP for API calls
- `websockets` — WebSocket client for signaling
- `av`, `numpy` — video frame handling

### System (Docker image installs these)
- `ffmpeg` — re-encode H264 + push RTSP
- `mediamtx` — bundled RTSP server (latest release fetched at build time)
- `s6-overlay` — process supervisor for running mediamtx + bridge together

## License

MIT — see [LICENSE](LICENSE).
