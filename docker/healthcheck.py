#!/usr/bin/env python3
"""Container healthcheck for birdfy-bridge — grace-period publish check.

History (see memory project_healthcheck_rtsp_port): the healthcheck used to be a
bare TCP connect to the RTSP port. That never flapped, but it reported HEALTHY
even when the bridge connected yet never published (e.g. ffmpeg hung opening a
second input -> MediaMTX "no stream is available"). A strict publish check was
rejected earlier because the stream legitimately tears down and republishes
during normal Frigate/WebRTC reconnects and feeder-camera sleep, so it would flap
unhealthy and could restart a working container.

This resolves that tension: it checks whether MediaMTX's 'birdfy' path is actively
publishing (ready:true via the local control API), but only reports UNHEALTHY
after the stream has been continuously down for a sustained grace window. A brief
reconnect/sleep stays healthy; a true never-came-up or long-dead stream goes
unhealthy.

Layers:
  1. MediaMTX dead (control API unreachable)      -> immediate UNHEALTHY (no grace).
  2. 'birdfy' publishing (ready:true)             -> HEALTHY, reset down-timer.
  3. Not publishing, but down < grace window      -> HEALTHY (tolerate reconnects).
  4. Not publishing, and down >= grace window     -> UNHEALTHY.

Stdlib only (no pip deps in the healthcheck path). Tunables via env:
  HEALTHCHECK_PATH        RTSP/MediaMTX path name        (default: birdfy)
  HEALTHCHECK_API         MediaMTX control API base      (default: http://127.0.0.1:9997)
  HEALTHCHECK_GRACE_SEC   down-duration before unhealthy (default: 300 = 5 min)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

PATH_NAME = os.getenv("HEALTHCHECK_PATH", os.getenv("RTSP_PATH", "birdfy"))
API_BASE = os.getenv("HEALTHCHECK_API", "http://127.0.0.1:9997").rstrip("/")
GRACE_SEC = int(os.getenv("HEALTHCHECK_GRACE_SEC", "300"))

# Persisted "stream first seen down at" timestamp. /tmp is fine — it only needs to
# survive between healthcheck invocations (every ~30s), not across restarts; a
# container restart legitimately resets the grace window anyway.
_STATE_FILE = "/tmp/birdfy_healthcheck_down_since"


def _api_alive() -> bool:
    """True iff the MediaMTX control API responds — uses HTTP, not a raw RTSP
    TCP connect, so it never generates noise in the MediaMTX RTSP log."""
    try:
        with urllib.request.urlopen(f"{API_BASE}/v3/config/global/get", timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _path_ready() -> bool:
    """True iff MediaMTX reports the path exists and is actively publishing.

    Any API error (API down, path absent, malformed) is treated as "not ready"
    so the grace logic decides — we never crash the healthcheck on API hiccups.
    """
    url = f"{API_BASE}/v3/paths/get/{PATH_NAME}"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    # MediaMTX v3 Path: {"name": ..., "ready": bool, "readyTime": str|null, ...}.
    # `ready` is marked deprecated in newer MediaMTX in favor of `readyTime` (a
    # non-null timestamp once publishing), so accept either to stay forward-compatible.
    if data.get("ready") is True:
        return True
    return data.get("readyTime") is not None


def _read_down_since() -> float | None:
    try:
        with open(_STATE_FILE) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_down_since(ts: float) -> None:
    try:
        with open(_STATE_FILE, "w") as f:
            f.write(str(ts))
    except OSError:
        pass


def _clear_down_since() -> None:
    try:
        os.unlink(_STATE_FILE)
    except OSError:
        pass


def main() -> int:
    # Layer 1: MediaMTX itself must be up. If it's not, nothing can publish — hard
    # fail immediately (no grace). Uses the control API rather than a raw RTSP TCP
    # connect so it doesn't generate noise in the MediaMTX RTSP log.
    if not _api_alive():
        print(f"UNHEALTHY: MediaMTX control API {API_BASE} unreachable")
        return 1

    # Layer 2: actively publishing -> healthy, reset the down-timer.
    if _path_ready():
        _clear_down_since()
        print(f"HEALTHY: path '{PATH_NAME}' is publishing (ready)")
        return 0

    # Layers 3/4: not publishing — tolerate up to GRACE_SEC of downtime so normal
    # reconnects/camera-sleep don't flap us unhealthy.
    now = time.time()
    down_since = _read_down_since()
    if down_since is None:
        down_since = now
        _write_down_since(down_since)
    down_for = now - down_since
    if down_for < GRACE_SEC:
        print(
            f"HEALTHY (grace): path '{PATH_NAME}' not publishing for "
            f"{down_for:.0f}s < {GRACE_SEC}s grace"
        )
        return 0
    print(
        f"UNHEALTHY: path '{PATH_NAME}' not publishing for {down_for:.0f}s "
        f">= {GRACE_SEC}s grace"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
