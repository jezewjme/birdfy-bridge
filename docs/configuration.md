# Configuration

All configuration is via environment variables (set them in `.env` for the bundled `compose.yaml`).

| Env var          | Required | Description |
|------------------|----------|-------------|
| `BIRDFY_EMAIL`   | Yes      | Netvue / Birdfy account email |
| `BIRDFY_PASSWORD`| Yes      | Account password (plain text, MD5'd internally) |
| `DEVICE_ID`      | No       | Camera serial number; defaults to the first device on the account (see [Finding your DEVICE_ID](#finding-your-device_id)) |
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
| `BIRDFY_FRAME_RATE`        | No | Seed for ffmpeg's raw-H264 demuxer fps guess (`-r`, default: `9`). No longer the A/V-sync-critical value it once was — output timing now comes from input wallclock timestamps on paced writes, not a fixed CFR. See [Stream timestamps](protocol.md#stream-timestamps-broken-timing--frigate-fps-cap-kill--av-drift). |
| `BIRDFY_JITTER_CAPACITY`   | No | aiortc video jitter buffer size, power of 2 (default: `2048`). Widened to fit large keyframes. |
| `BIRDFY_RTP_HISTORY_SIZE`  | No | NACK missing-packet tracking window (default: `1024`). |
| `BIRDFY_NACK_INTERVAL_MS`  | No | Periodic re-NACK interval in ms (default: `30`; `0` disables re-NACK). |
| `BIRDFY_NACK_MAX_RETRIES`  | No | Max re-NACK re-sends per missing packet (default: `12`). |
| `BIRDFY_STUB_VIDEO_DECODE` | No | `0` to restore aiortc's real H264 decoder. Default on (no-op decoder) — we forward via `-c copy` and never use decoded frames, so this silences the decoder's `Invalid data` warnings on this camera's bitstream. |
| `MQTT_HOST`                | No | MQTT broker host. **Unset = MQTT disabled**; the bridge runs exactly as before. See [Home Assistant control & sensors](home-assistant.md). |
| `MQTT_PORT`                | No | Broker port (default: `1883`). |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | No | Broker credentials. Omit both for an anonymous broker. |
| `MQTT_BASE_TOPIC`          | No | Topic prefix for state/command topics (default: `birdfy`). |
| `MQTT_DISCOVERY_PREFIX`    | No | Home Assistant MQTT-discovery prefix (default: `homeassistant`). |
| `BIRDFY_MODE`              | No | First-boot mode before HA publishes one: `always_on` / `auto` / `off` (default: `auto`). The HA **Mode** select overrides this at runtime. |
| `BIRDFY_OFF_POLL_SECONDS`  | No | In `off` mode, refresh the battery/online sensors this often (default: `0` = don't poll; leave the camera alone). |
| `BIRDFY_OFF_SENTINEL`      | No | Path to the off-mode sentinel file the bridge touches while in `off` mode (default: `/tmp/birdfy_mode_off`). The container healthcheck treats its presence as HEALTHY so it doesn't restart an intentionally-paused bridge. Must match the healthcheck's value. See [Operations](operations.md). |

## Finding your DEVICE_ID

`DEVICE_ID` is optional — if it's unset (or doesn't match), the bridge falls back to the **first device on the account** and logs a warning listing every device with its serial number:

```
Device '1234567890123456' not found — falling back to first device.
Available: ['1234567890123456 / addxSn=AB12CD34 (Bamboo Feeder)', ...]
```

If the account has more than one camera, copy the matching `serialNumber` into `.env` to pin the right one.

## Token persistence

On every start the bridge tries to **reuse a cached login token** instead of calling `/users/login/v2` again. A fresh login is what makes Netvue send a *"new device logged in"* email, so reusing the token keeps those from arriving on each restart.

How it works: after a successful login the token (plus region/endpoint) is written to `BIRDFY_STATE_DIR/.birdfy_auth_cache.json` (mode `0600`), alongside the persisted device UDID. On the next start the bridge validates the cached token with a device-list call and only falls back to a fresh login if the token is missing, belongs to a different account, or is rejected. A transient network error during validation does **not** trigger a re-login — the retry loop waits and tries the cache again.

For this to survive container recreation, `BIRDFY_STATE_DIR` must point at a persistent path. The bundled `compose.yaml` sets `BIRDFY_STATE_DIR=/data` backed by a named `birdfy-state` volume, so it works out of the box. If you run the image without that compose file, add an equivalent volume (e.g. `-v birdfy-state:/data -e BIRDFY_STATE_DIR=/data`), or the token (and UDID) will be lost on every recreate and the emails return.

Set `NVS_NO_TOKEN_CACHE=1` to opt out and always log in fresh.
