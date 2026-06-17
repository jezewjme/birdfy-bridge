# Home Assistant control & sensors

Optional. Set `MQTT_HOST` (and credentials if your broker needs them) and the bridge connects to MQTT, **auto-creates** a device in Home Assistant via MQTT Discovery, and exposes:

- **Mode** (`select`): `always_on` / `auto` / `off` — controls the bridge live.
- **Battery** (`sensor`, `%`): the camera's last-reported charge. Alert on this in HA and a dying battery never blindsides you again.
- **Online / Awake / Charging** (`binary_sensor`).

If `MQTT_HOST` is unset, none of this runs and the bridge behaves exactly as before. MQTT outages never affect streaming — the bridge keeps running in whatever mode it last had (or `BIRDFY_MODE`).

See [Configuration](configuration.md) for the full list of `MQTT_*` and `BIRDFY_MODE` env vars.

## The three modes

| Mode | Behavior | Use for |
|------|----------|---------|
| `always_on` | Connect whenever the camera is online. | Max coverage. |
| `auto` | Connect, but defer to the camera's **native dormancy schedule** (set in the Birdfy app). During the overnight window the camera reports offline and the bridge backs off — no wake-pokes. | Saving battery overnight while keeping daytime coverage. |
| `off` | Pause the connect loop entirely; the camera is left alone. | Hard stop. |

**Why `auto` saves the most battery:** the bridge is only a *viewer* — it can't stop the camera's own wake/detect cycles. Setting the camera's **native overnight dormancy in the Birdfy app** is what actually conserves power; `auto` simply respects the resulting offline window instead of churning reconnect attempts against a sleeping cam. The mode is read from a *retained* MQTT topic, so it survives bridge restarts.

> **`always_on` vs `auto` — why they look the same in the bridge:** the streaming *schedule* lives in the **Birdfy app** (the camera's native dormancy), not in the bridge. There is no bridge-side "stream from 7am–9pm" setting, and adding one would only fight the camera's own schedule and waste battery waking a cam the app wants dormant. So both modes run the same connect-and-backoff loop and converge in observed behavior: when the camera is online they stream; when it reports offline (overnight dormancy) they back off without wake-pokes. The difference is **intent** — `always_on` says "stream whenever reachable, max coverage"; `auto` says "I've set an overnight window in the app and just want the bridge to follow it." To change *when* the feeder streams, edit the dormancy schedule in the Birdfy app. Only `off` changes the bridge's behavior directly (it stops connecting entirely, and as of now also tears down a live session the moment you switch to it).

> **Battery polling in `off` mode:** reading battery requires a `selectsingledevice` call. This *may* be a passive cloud read (the state the camera last reported) that costs no camera battery — but that isn't confirmed, so `off` mode does **not** poll by default (`BIRDFY_OFF_POLL_SECONDS=0`) and the HA battery sensor simply holds its last value. If you confirm the read is passive on a healthy battery, set `BIRDFY_OFF_POLL_SECONDS=1200` for a live overnight battery sensor.

## Connecting to Home Assistant's Mosquitto

Point `MQTT_HOST` at the broker reachable from the bridge container (typically the HA host's LAN IP, or a shared Docker network's broker service name), set `MQTT_PORT` (1883) and `MQTT_USERNAME`/`MQTT_PASSWORD` for a dedicated MQTT user. With HA's MQTT integration enabled, the **Birdfy** device and its entities appear automatically — no YAML.
