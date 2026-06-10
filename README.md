# birdfy-bridge

An interoperability tool that converts a Birdfy / Netvue camera's WebRTC stream into RTSP so it can be consumed by Frigate, go2rtc, VLC, Home Assistant, or anything else that speaks RTSP.

> **Unofficial, unaffiliated client.** Not endorsed by, sponsored by, or affiliated with Netvue Inc., Birdfy, Vicohome, or Addx. "Birdfy", "Netvue", "Vicohome", and "Addx" are trademarks of their respective owners and are used here only nominatively to identify the cameras this tool interoperates with.
>
> This project is for use with cameras **you own**. Using it may violate Netvue's Terms of Service and could result in your Birdfy account being suspended or terminated — use a **dedicated account**, not your primary one. The project ships with no warranty (see [LICENSE](LICENSE)).

## Status

**Working** — the Birdfy Feeder Bamboo streams reliably into Frigate over RTSP via the bundled MediaMTX. The WebRTC handshake, keyframe recovery, and timestamp handling are all confirmed against live captures. Known rough edges are listed under [What's not done](#whats-not-done).

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

#### Recommended setup: use a downscaled detect substream

The bridge outputs a single **1920×1080** H264 stream. If you point Frigate's `detect` role straight at it, Frigate's ffmpeg has to **software-decode full 1080p** for every analyzed frame — which shows up as high ffmpeg CPU (e.g. a "high FFmpeg CPU usage" warning). The camera only offers one WebRTC stream, so there's no native substream to fall back on.

The fix is to let **go2rtc** create a downscaled detect stream from the single bridge feed, and split Frigate's roles so `detect` reads the small stream while `record`/`live` keep full resolution:

```yaml
go2rtc:
  streams:
    # Full-res stream — one connection to the bridge.
    birdfy:
      - rtsp://birdfy-bridge:8554/birdfy
    # Downscaled detect substream. Sourcing from the go2rtc "birdfy" stream
    # (not the bridge URL) reuses that single bridge connection; go2rtc just
    # fans it out. Use #hardware if your host has a GPU (e.g. Intel VAAPI);
    # drop it to scale on CPU (still cheap at 640x360).
    birdfy_sub:
      - "ffmpeg:birdfy#video=h264#hardware#width=640#height=360"

cameras:
  BirdfyFeeder:
    ffmpeg:
      hwaccel_args: preset-vaapi   # if you have a supported GPU
      inputs:
        - path: rtsp://127.0.0.1:8554/birdfy_sub
          input_args: preset-rtsp-restream
          roles: [detect]
        - path: rtsp://127.0.0.1:8554/birdfy
          input_args: preset-rtsp-restream
          roles: [record]
    detect:
      width: 640
      height: 360
      fps: 5
```

This keeps a **single** connection to the bridge (go2rtc derives the substream internally) and drops detect-side CPU dramatically, since the detector decodes a ~9× smaller frame. Reload the Frigate config after editing — it doesn't auto-reload.

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
| `NOISY_LOG_LEVEL`| No       | Floor for chatty aiortc/aioice/websockets loggers (default: `WARNING`). These log every RTP packet at DEBUG; set to `DEBUG` to get full packet traces when debugging. |
| `LOG_FILE`       | No       | Path for file log (default: `birdfy-bridge.log`; empty = stdout only) |
| `NVS_UCID`       | No       | App client ID (default: `513774810c`) |
| `NVS_UDID`       | No       | Stable device UUID for signing (auto-generated and persisted per-host if unset) |
| `BIRDFY_STATE_DIR`         | No | Directory for the persisted UDID + cached auth token (default: home dir; `compose.yaml` sets `/data`, backed by a volume). See [Token persistence](#token-persistence). |
| `NVS_NO_TOKEN_CACHE`       | No | Set to disable token caching and always do a fresh login. |
| `NVS_NO_TOKEN_REFRESH`     | No | Set to disable the `refreshToken`-based renewal on token expiry (falls straight back to a full login). |
| `BIRDFY_AUDIO`             | No | `0` to disable PCMU audio passthrough (video-only). Default on. POSIX-only; auto-disables elsewhere. |
| `BIRDFY_FRAME_RATE`        | No | Constant output frame rate fed to ffmpeg (default: `15`, the camera's negotiated rate). See [RTP receive-path quirks](#rtp-receive-path-quirks). |
| `BIRDFY_JITTER_CAPACITY`   | No | aiortc video jitter buffer size, power of 2 (default: `2048`). Widened to fit large keyframes. |
| `BIRDFY_RTP_HISTORY_SIZE`  | No | NACK missing-packet tracking window (default: `1024`). |
| `BIRDFY_NACK_INTERVAL_MS`  | No | Periodic re-NACK interval in ms (default: `30`; `0` disables re-NACK). |
| `BIRDFY_NACK_MAX_RETRIES`  | No | Max re-NACK re-sends per missing packet (default: `12`). |

## Token persistence

On every start the bridge tries to **reuse a cached login token** instead of calling `/users/login/v2` again. A fresh login is what makes Netvue send a *"new device logged in"* email, so reusing the token keeps those from arriving on each restart.

How it works: after a successful login the token (plus region/endpoint) is written to `BIRDFY_STATE_DIR/.birdfy_auth_cache.json` (mode `0600`), alongside the persisted device UDID. On the next start the bridge validates the cached token with a device-list call and only falls back to a fresh login if the token is missing, belongs to a different account, or is rejected. A transient network error during validation does **not** trigger a re-login — the retry loop waits and tries the cache again.

For this to survive container recreation, `BIRDFY_STATE_DIR` must point at a persistent path. The bundled `compose.yaml` sets `BIRDFY_STATE_DIR=/data` backed by a named `birdfy-state` volume, so it works out of the box. If you run the image without that compose file, add an equivalent volume (e.g. `-v birdfy-state:/data -e BIRDFY_STATE_DIR=/data`), or the token (and UDID) will be lost on every recreate and the emails return.

Set `NVS_NO_TOKEN_CACHE=1` to opt out and always log in fresh.

## Security warnings

- **`.env` contains your full Birdfy account password.** Anyone with that file can log into your account and reach every camera, doorbell, or other Netvue device on it. Keep it out of backups, repos, and shared drives. Use a dedicated Birdfy account for the bridge if at all possible.
- **`BIRDFY_STATE_DIR/.birdfy_auth_cache.json` contains a usable login token.** It's written `0600` and git-ignored, but treat the state volume as sensitive — a leaked token grants account access until it expires. Delete the file (or the volume) to force a fresh login.
- **The bundled MediaMTX has no authentication.** This is fine on a trusted LAN. **Do not expose port 8554 to the public internet** — anyone who can reach it can pull your stream. If you must, configure `publishUser` / `readUser` in `docker/mediamtx.yml` and put the container behind a reverse proxy.
- **DEBUG logs may still contain non-secret identifying data** (device serials, region info, ICE server URLs). Bearer tokens, signed URLs, refresh tokens, and AWS credentials are redacted, but treat DEBUG logs as semi-sensitive when sharing them in bug reports.
- **Netvue's API is reverse-engineered, not contracted.** It can change at any time. The bridge fails loudly when it does; please open an issue if your account suddenly stops authenticating or device-list returns 4xx.

## What's not done

- **KVS WebRTC path**: `onAddx: false` devices (some newer outdoor cameras) use AWS Kinesis Video Streams. Currently raises `NotImplementedError`. Contributions welcome.
- **Initial keyframe latency**: After the data channel opens, the camera takes a few seconds to send the first keyframe. The bridge sends RTCP PLI/FIR to nudge it, but the first decodable frame still lags connection by a few seconds.
- **Token refresh**: On token expiry the bridge attempts a `refreshToken`-based renewal before falling back to a full re-login (which is what triggers Netvue's "new device logged in" email). The exact refresh endpoint isn't confirmed from packet captures, so the renewal tries a few plausible request shapes and is best-effort — the full login backstops it. Disable with `NVS_NO_TOKEN_REFRESH=1`.
- **Audio**: The camera's PCMU (G.711 µ-law) audio track is muxed into the RTSP output with `-c:a copy` (no re-encode). Requires a POSIX host (uses `pass_fds`); degrades to video-only on other platforms. Disable with `BIRDFY_AUDIO=0`.
- **Publish-level healthcheck**: The container healthcheck verifies the `birdfy` path is actively publishing, with a grace window to tolerate normal reconnects (see [Container healthcheck](#container-healthcheck)).

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

6. **Video** received as H264 via aiortc. We do **not** decode/re-encode: aiortc's
   libavcodec H264 decoder can't decode this camera's bitstream, and Frigate
   re-encodes anyway. Instead we tap aiortc's jitter buffer between depayload and
   decode, pull the reassembled Annex B frames, and pipe them to an
   `ffmpeg -c copy -f rtsp` passthrough (see [`_rtp_forwarder.py`](_rtp_forwarder.py)).
   No decode, no re-encode. See [RTP receive-path quirks](#rtp-receive-path-quirks)
   for the keyframe-recovery and timestamp fixes that make this reliable.

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

## RTP receive-path quirks

Two more load-bearing fixes live in [`_aiortc_media_patches.py`](_aiortc_media_patches.py) and [`_rtp_forwarder.py`](_rtp_forwarder.py); both were confirmed against captured logs.

### Keyframe corruption (garbage frames with no start code)

The camera's keyframes are large (~25–52 KB ≈ 50–110 RTP packets of FU-A fragments). aiortc's video jitter buffer defaults to `capacity=128`, so one keyframe nearly fills it; any reorder or a not-yet-recovered lost packet evicts the **head** of the keyframe (the FU-A start fragment carrying the NAL header + Annex B start code), and the surviving fragments reassemble into headerless garbage that nothing can decode. aiortc also NACKs a missing packet only **once** and only tracks gaps within `RTP_HISTORY_SIZE=128`.

`_aiortc_media_patches.py` fixes this by (1) widening the video jitter buffer (128 → 2048), (2) widening the NACK tracking window (128 → 1024), and (3) adding a **periodic re-NACK** loop per video receiver that re-requests still-missing sequence numbers until they arrive — which is what actually recovers a dropped keyframe-head fragment. All four parameters are env-tunable (see [Configuration](#configuration)). It also logs the camera's advertised RTCP feedback (whether `nack` is supported), every re-NACK, and a per-session corruption tally for debugging.

### Stream drops (broken timestamps → Frigate fps-cap kill → 404 cascade)

The passthrough ffmpeg originally stamped frames at arrival wall-clock time. But NACK-recovered and reordered frames arrive out of order, so wall-clock DTS went backwards (`Non-monotonous DTS` in ffmpeg logs) and the stream looked like ~30 fps to Frigate. Frigate's fps-cap watchdog then killed its reader, which tore down the RTSP session, broke our ffmpeg's pipe, and dropped the MediaMTX path — after which Frigate's restart hit `404 Not Found` in a restart cascade.

Fixed in `_rtp_forwarder.py` by declaring the input as constant `BIRDFY_FRAME_RATE` fps (default 15, the camera's negotiated rate) and forcing CFR output, so ffmpeg emits clean monotonic timestamps regardless of how jittery our frame delivery is.

### Container healthcheck

The Docker `HEALTHCHECK` uses MediaMTX's control API (`http://127.0.0.1:9997`) rather than a raw TCP connect to the RTSP port. This avoids generating noise in the MediaMTX RTSP log (each raw TCP connect appeared as an `[::1] opened / closed: EOF` pair).

The healthcheck operates in layers (see [`docker/healthcheck.py`](docker/healthcheck.py)):

1. **MediaMTX dead** (control API unreachable) → immediate `UNHEALTHY`, no grace.
2. **`birdfy` path publishing** (`ready: true`) → `HEALTHY`, reset down-timer.
3. **Not publishing, down < grace window** → `HEALTHY` (tolerate reconnects and camera sleep).
4. **Not publishing, down ≥ grace window** → `UNHEALTHY`.

The grace window (default 5 minutes, `HEALTHCHECK_GRACE_SEC`) prevents the normal WebRTC reconnect / feeder-camera sleep cycle from restarting a working container. A container that never publishes, or whose stream dies and stays dead, will eventually go `UNHEALTHY`.

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
- `aiortc` — WebRTC library (handles SDP, ICE, H264 depayload). Patched at runtime; see [RTP receive-path quirks](#rtp-receive-path-quirks).
- `aiohttp` — async HTTP for API calls
- `websockets` — WebSocket client for signaling
- `av`, `numpy` — pulled in by aiortc (we don't decode video ourselves)

### System (Docker image installs these)
- `ffmpeg` — H264 passthrough (`-c copy`) + push RTSP (no re-encode)
- `mediamtx` — bundled RTSP server (latest release fetched at build time)
- `s6-overlay` — process supervisor for running mediamtx + bridge together

## License

MIT — see [LICENSE](LICENSE).
