# Operations: limitations, caveats & healthcheck

## Limitations and caveats

- **KVS WebRTC path (not implemented)**: `onAddx: false` devices (some newer outdoor cameras) use AWS Kinesis Video Streams. The bridge exits with an error for these. Contributions welcome.
- **Initial keyframe latency**: After the data channel opens, the camera takes a few seconds to send the first keyframe. The bridge sends RTCP PLI/FIR to nudge it, but the first decodable frame still lags connection by a few seconds.
- **Token refresh is best-effort**: On token expiry the bridge attempts a `refreshToken`-based renewal before falling back to a full re-login (which is what triggers Netvue's "new device logged in" email). The exact refresh endpoint isn't confirmed from packet captures, so the renewal tries a few plausible request shapes — the full login backstops it. Disable with `NVS_NO_TOKEN_REFRESH=1`.
- **Audio is POSIX-only**: The camera's PCMU (G.711 µ-law) audio track is muxed into the RTSP output with `-c:a copy` (no re-encode). Requires a POSIX host (uses `pass_fds`); degrades to video-only on other platforms. Disable with `BIRDFY_AUDIO=0`.
- **Healthcheck grace window**: The container healthcheck verifies the `birdfy` path is actively publishing, with a grace window to tolerate normal reconnects — a dead stream takes up to ~5 minutes to surface as unhealthy (see [Container healthcheck](#container-healthcheck)).
- **Offline / sleeping cameras**: Battery cameras sleep and report `online: 0` between events (and a dead battery takes them fully offline). Before each connection the bridge reads the device's live state and, if it's offline, logs `Device state: online=0 … battery=N%` and **skips the WebRTC handshake**, backing off instead of churning futile reconnects (which would only burn battery waking the cloud). This is expected — the bridge resumes automatically when the camera returns. If MQTT is configured, battery/online are also exposed as HA sensors so you can alert before it dies.

## Container healthcheck

The Docker `HEALTHCHECK` uses MediaMTX's control API (`http://127.0.0.1:9997`) rather than a raw TCP connect to the RTSP port. This avoids generating noise in the MediaMTX RTSP log (each raw TCP connect appeared as an `[::1] opened / closed: EOF` pair).

The healthcheck operates in layers (see [`docker/healthcheck.py`](../docker/healthcheck.py)):

0. **Bridge intentionally `off`** (off sentinel present) → `HEALTHY`, short-circuit. In `off` mode the bridge never publishes by design, so without this the grace window below would expire and Docker would restart a perfectly healthy container. The bridge touches `BIRDFY_OFF_SENTINEL` (default `/tmp/birdfy_mode_off`) while off and removes it on any non-off iteration.
1. **MediaMTX dead** (control API unreachable) → immediate `UNHEALTHY`, no grace.
2. **`birdfy` path publishing** (`ready: true`) → `HEALTHY`, reset down-timer.
3. **Not publishing, down < grace window** → `HEALTHY` (tolerate reconnects and camera sleep).
4. **Not publishing, down ≥ grace window** → `UNHEALTHY`.

The grace window (default 5 minutes, `HEALTHCHECK_GRACE_SEC`) prevents the normal WebRTC reconnect / feeder-camera sleep cycle from restarting a working container. A container that never publishes, or whose stream dies and stays dead, will eventually go `UNHEALTHY`.
