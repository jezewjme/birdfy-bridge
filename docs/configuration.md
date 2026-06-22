# Configuration

All configuration is via environment variables (set them in `.env` for the bundled `compose.yaml`). The table below covers the options you'd normally set; a handful of low-level internals are documented separately under [Advanced tuning](#advanced-tuning) and are not required.

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
| `BIRDFY_FRAME_RATE`        | No | Constant frame rate the copied stream is stamped at via `setts` (default: `8.6`, the measured delivered rate; fractional values OK). Must match the real delivered rate: too high trips Frigate's fps-cap, and any mismatch drifts video against the audio sample clock. See [Stream timestamps](protocol.md#stream-timestamps-broken-timing--frigate-fps-cap-kill--av-drift). |
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
| `BIRDFY_MODE`              | No | First-boot mode: `always_on` / `auto` / `off` (default: `auto`). The HA **Mode** select overrides this at runtime, and the chosen mode is persisted to `BIRDFY_STATE_DIR/.birdfy_mode` so it survives a container restart — so this env only applies before that file exists. Delete the file to let a changed `BIRDFY_MODE` take effect again. |
| `BIRDFY_OFF_POLL_SECONDS`  | No | In `off` mode, refresh the battery/online/awake/charging sensors this often, in seconds (default: `600` = every 10 min). This is a passive cloud read (no camera wake-poke), keeping the HA sensors live instead of frozen at the last stream's values. Set `0` to disable polling and leave the camera fully alone (sensors hold their last value). Leaving the option **blank** uses the default (a blank value is treated the same as unset), so you must set it to `0` explicitly to disable. |
| `BIRDFY_OFF_POLL_INITIAL_SECONDS` | No | When *entering* `off` mode, wait this long then do one poll before settling into the `BIRDFY_OFF_POLL_SECONDS` cadence, so the just-went-stale sensors correct promptly instead of after a full interval (default: `15`). `0` = poll immediately on entry. Only applies when `BIRDFY_OFF_POLL_SECONDS > 0`. |
| `BIRDFY_OFF_SENTINEL`      | No | Path to the off-mode sentinel file the bridge touches while in `off` mode (default: `/tmp/birdfy_mode_off`). The container healthcheck treats its presence as HEALTHY so it doesn't restart an intentionally-paused bridge. Must match the healthcheck's value. See [Operations](operations.md). |

## Advanced tuning

**None of these are required.** They're low-level internals with sane defaults that match this camera's behavior — you should not need to touch them for a normal install. They're documented here for debugging and for the rare camera/firmware that deviates from what the bridge was tuned against.

| Env var          | Required | Description |
|------------------|----------|-------------|
| `BIRDFY_AUDIO_FORMAT`   | No | ffmpeg `-f` input format for the camera's audio stream (default: `mulaw`). The Birdfy camera sends PCMU/8000, so `mulaw` is correct; only change it if a firmware revision switches codecs. |
| `BIRDFY_AUDIO_RATE`     | No | Audio sample rate in Hz fed to ffmpeg (default: `8000`). Must match what the camera actually sends — a mismatch pitches the audio and drifts it against the video clock. |
| `BIRDFY_AUDIO_CHANNELS` | No | Audio channel count (default: `1`). The camera is mono; raise only if a future model sends stereo. |
| `BIRDFY_DC_LABEL`       | No | WebRTC data-channel label used to send the start-live command (default: `webDataChannel`). This is what the camera's signaling expects; change only if a firmware revision renames the channel. |
| `BIRDFY_DC_PROTOCOL`    | No | WebRTC data-channel `protocol` field (default: empty). Left blank to match the camera; provided as an escape hatch if signaling ever requires a value. |
| `BIRDFY_DC_PAYLOADS`    | No | `\|`-separated list of start-live payloads the bridge tries in order until the camera starts streaming (default: a built-in list of `startLive`/`startlive` JSON and bare-string variants). Override only if a firmware revision expects a different command shape. |
| `BIRDFY_FORWARD_QUEUE`  | No | Max depth of the in-memory frame queue between the WebRTC receiver and the RTSP forwarder (default: `512`). The link is bursty; raise it if you see `queue depth` warnings in the logs, lower it to cap memory on constrained hosts. |
| `HEALTHCHECK_API`       | No | MediaMTX control-API base URL the container healthcheck probes (default: `http://127.0.0.1:9997`). Change only if you've moved MediaMTX's API off its default port. See [Operations](operations.md). |
| `HEALTHCHECK_PATH`      | No | RTSP/MediaMTX path name the healthcheck checks for an active publisher (default: falls back to `RTSP_PATH`, i.e. `birdfy`). Keep it in sync with `RTSP_PATH` if you change the stream path. |

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
