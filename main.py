"""
Birdfy RTSP bridge — main entry point.

Auth flow (reverse-engineered from my.birdfy.com web app, confirmed working):
  1. POST https://localweb.nvts.co/v1/users/login/v2 → token, userID, region, localEndpoint
     (skipped when a cached token from a previous run still validates — see
     birdfy_api.login_or_resume; avoids Netvue's "new device logged in" email)
  2. GET  {localEndpoint}/v1/devices/v3              → device list (find your camera by serial)
  3a. If device.onAddx == True (Birdfy Feeder Bamboo, Feeder, Cam, etc.):
      GET {localEndpoint}/v1/addx/token/v2 → ticket{signalServer, groupId, role, id, iceServer, ...}
      WebSocket to signaling URL → SDP offer/answer (mode: vicoo)
      Receive H264 → ffmpeg → RTSP push
  3b. If device.onAddx == False:
      POST {localEndpoint}/devices/{sn}/play provider=KVS_WEBRTC → AWS KVS credentials
      Use AWS Kinesis Video Streams WebRTC SDK (not yet implemented here)

Environment variables:
  BIRDFY_EMAIL         Netvue/Birdfy account email
  BIRDFY_PASSWORD      Netvue/Birdfy account password (plain text; MD5'd internally)
  DEVICE_ID            Camera serial number (e.g. "1234567890123456"). Optional — defaults to first device on the account.
  RTSP_OUTPUT          Full RTSP push URL. If unset, built from RTSP_HOST + RTSP_PATH.
  RTSP_HOST            RTSP server host:port (default: localhost:8554) — used only if RTSP_OUTPUT is unset.
  RTSP_PATH            RTSP stream path (default: birdfy) — used only if RTSP_OUTPUT is unset.
  LOG_LEVEL            DEBUG / INFO / WARNING (default: INFO)
  LOG_FILE             Path to file for logging output (default: birdfy-bridge.log; empty = stdout only)
  NOISY_LOG_LEVEL      Floor for chatty aiortc/aioice/websockets loggers (default: WARNING).
                       Set to DEBUG to get the per-packet firehose back.

  --- Optional: auth/token persistence (see birdfy_api.py) ---
  BIRDFY_STATE_DIR     Directory for the persisted UDID + cached auth token (default: home dir).
  NVS_NO_TOKEN_CACHE   Set to disable token caching (always do a fresh login).
  NVS_NO_TOKEN_REFRESH Set to disable refreshToken-based renewal on expiry (full login instead).

  --- Optional: media (see _rtp_forwarder.py / _aiortc_media_patches.py) ---
  BIRDFY_AUDIO         0 to disable PCMU audio passthrough (default: on; POSIX-only).
  BIRDFY_FRAME_RATE    Constant output frame rate the copied stream is stamped at
                       (default: 8.6, the measured delivered rate; float OK). Must
                       match the camera's real delivered rate: a mismatch drifts
                       video against audio (see _rtp_forwarder.py).
  BIRDFY_JITTER_CAPACITY / BIRDFY_RTP_HISTORY_SIZE / BIRDFY_NACK_INTERVAL_MS /
  BIRDFY_NACK_MAX_RETRIES   Keyframe-recovery tunables; see _aiortc_media_patches.py.

  --- Optional: MQTT control + HA sensors (see mqtt_control.py) ---
  MQTT_HOST            Broker host. UNSET = MQTT off; bridge runs exactly as before.
  MQTT_PORT            Broker port (default: 1883).
  MQTT_USERNAME        Broker username (optional; omit for anonymous).
  MQTT_PASSWORD        Broker password (optional).
  MQTT_BASE_TOPIC      Topic prefix for state/command (default: birdfy).
  MQTT_DISCOVERY_PREFIX  HA MQTT-discovery prefix (default: homeassistant).
  BIRDFY_MODE          First-boot mode: always_on | auto | off (default: auto).
                       HA's "Mode" select overrides at runtime, and the chosen mode
                       is persisted (BIRDFY_STATE_DIR/.birdfy_mode) so it survives a
                       restart — so this env only applies before that file exists.
                       Delete the file to let a changed BIRDFY_MODE take effect again.
  BIRDFY_OFF_POLL_SECONDS  In `off` mode, refresh battery/online/awake/charging
                       sensors this often via a passive cloud read (default: 600 =
                       every 10 min). Keeps HA sensors live instead of frozen at the
                       last stream's values. Set 0 to disable and leave camera alone.
  BIRDFY_OFF_POLL_INITIAL_SECONDS  On entering `off`, wait this long then poll once
                       before settling into the steady cadence (default: 15), so the
                       just-stale sensors correct promptly. Only used when polling on.
  BIRDFY_OFF_SENTINEL  Path to the off-mode sentinel the healthcheck honors as
                       HEALTHY (default: /tmp/birdfy_mode_off). Must match
                       docker/healthcheck.py.
  BIRDFY_SESSION_STATE_POLL_SECONDS  While a stream is live, re-read battery/online
                       state this often so HA sensors don't go stale on long
                       sessions (default: 60; 0 = only publish at session start).

  --- Optional overrides for NVS signing ---
  NVS_UCID             App client ID (default: 513774810c)
  NVS_UDID             Device UUID for signing (auto-generated and persisted if not set)
"""
import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

from birdfy_api import (
    DeviceOfflineError,
    device_state_summary,
    get_addx_ticket,
    login_or_resume,
    select_single_device,
    stop_live,
)
from mqtt_control import MqttConfig, MqttControl
from webrtc_client import connect_and_stream

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
log_file = os.getenv("LOG_FILE", "birdfy-bridge.log")

log_handlers = [logging.StreamHandler(stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False))]
if log_file:
    log_path = Path(log_file)
    if log_path.parent:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handlers.append(logging.FileHandler(log_path, mode="a", encoding="utf-8"))

logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=log_handlers,
    force=True,
)

# Tame third-party per-packet/per-frame log spam. aiortc.rtcrtpreceiver logs
# EVERY RTP packet at DEBUG (~50k lines/min, ~99% of the log volume); the other
# aiortc/aioice/websockets DEBUG streams are similarly chatty. Even when we run
# our own code at DEBUG we almost never want that firehose, so pin these noisy
# libraries to a higher floor. Override the floor with NOISY_LOG_LEVEL=DEBUG to
# get the full packet trace back for deep debugging.
_noisy_log_level = os.getenv("NOISY_LOG_LEVEL", "WARNING").upper()
for _noisy in (
    "aiortc.rtcrtpreceiver",
    "aiortc.rtcrtpsender",
    "aiortc.rtcdtlstransport",
    "aiortc.rtcsctptransport",
    "aiortc.rtcdatachannel",
    "aiortc.rtcicetransport",
    "aiortc.rtcpeerconnection",
    "aioice.ice",
    "aioice.turn",
    "websockets.client",
    "websockets.server",
):
    logging.getLogger(_noisy).setLevel(
        getattr(logging, _noisy_log_level, logging.WARNING)
    )

logger = logging.getLogger("main")

BIRDFY_EMAIL    = os.environ["BIRDFY_EMAIL"]
BIRDFY_PASSWORD = os.environ["BIRDFY_PASSWORD"]
DEVICE_ID       = os.getenv("DEVICE_ID", "")
RTSP_OUTPUT     = os.getenv("RTSP_OUTPUT") or (
    f"rtsp://{os.getenv('RTSP_HOST', 'localhost:8554')}/{os.getenv('RTSP_PATH', 'birdfy')}"
)


def _pick_device(devices: list) -> dict | None:
    """Select the target device: DEVICE_ID match if set, else first device.

    Returns None only when the account has no devices. Logs a warning and falls
    back to the first device when DEVICE_ID is set but not found.
    """
    if DEVICE_ID:
        for device in devices:
            if device.get("serialNumber") == DEVICE_ID or device.get("addxSn") == DEVICE_ID:
                return device
        available = [
            f"{d.get('serialNumber')} / addxSn={d.get('addxSn')} ({d.get('deviceName')})"
            for d in devices
        ]
        logger.warning(
            f"Device {DEVICE_ID!r} not found — falling back to first device. "
            f"Available: {available}"
        )
    if not devices:
        return None
    target = devices[0]
    logger.info(f"Using device: {target.get('deviceName')!r} sn={target.get('serialNumber')}")
    return target


async def run_once(mqtt: MqttControl | None = None):
    # Step 1+2: Authenticate (reusing a cached token if still valid) and fetch
    # the device list. login_or_resume avoids a fresh /users/login/v2 — and the
    # "new device logged in" email it triggers — when a cached token still works,
    # and returns the device list from the same validation call.
    logger.info(f"Authenticating as {BIRDFY_EMAIL} ...")
    auth_data, devices = await login_or_resume(BIRDFY_EMAIL, BIRDFY_PASSWORD)
    user_id = str(auth_data.get("userID", ""))
    logger.info(f"Authenticated — userID={user_id} region={auth_data.get('region')}")

    target = _pick_device(devices)
    if target is None:
        raise RuntimeError("No devices found on this account.")

    logger.info(
        f"Device found: {target.get('deviceName')!r} sn={target['serialNumber']} "
        f"addxSn={target.get('addxSn')} onAddx={target.get('onAddx')} region={target.get('region')}"
    )

    # Now that the device is identified, bind MQTT to its serial/name and start
    # the control task (idempotent — only the first call does anything).
    if mqtt is not None:
        mqtt.configure(str(target["serialNumber"]), target.get("deviceName") or "Birdfy")

    on_addx = target.get("onAddx", False)

    if on_addx:
        # Step 3a: Addx WebRTC path. Browser HAR shows the per-session order is:
        #   selectsingledevice  → getWebrtcTicket → WS attempt
        #   on failure: stoplive → getWebrtcTicket → WS retry (new traceId)
        # We mirror it: do the select once on session start, then let
        # connect_and_stream's internal retry handle stoplive+new-ticket.
        device_region = target.get("region") or auth_data.get("region")
        a4x_user_id = str(auth_data.get("userID", ""))
        serial = target["serialNumber"]

        logger.info(f"Device uses Addx WebRTC — fetching ticket (region={device_region}) ...")
        ticket = await get_addx_ticket(auth_data, device=target, device_region=device_region)
        state = await select_single_device(ticket)

        # selectsingledevice carries live device state. Log battery on every
        # attempt (so it's visible in the bridge log / status), publish it to
        # MQTT for HA, and bail before the WebRTC handshake if the camera is
        # offline — a sleeping/dead battery cam will never send PEER_IN, so
        # attempting it just churns wake-pokes and burns ~13s per failed session.
        # DeviceOfflineError routes to the backoff path in main() without
        # counting as a connection failure.
        summary = device_state_summary(state)
        if mqtt is not None:
            mqtt.publish_state(summary)
        batt = summary["battery_level"]
        charging = summary["is_charging"]
        batt_str = f"{batt}%" if batt is not None else "unknown"
        charge_str = (
            " (charging)" if charging == 1 else "" if charging == 0 else ""
        )
        logger.info(
            "Device state: online=%s awake=%s battery=%s%s",
            summary["online"], summary["awake"], batt_str, charge_str,
        )
        # online is None when the field was missing/unparseable — only skip on a
        # definite offline (online == 0), never on unknown, so a shape change in
        # the API can't silently stop us from ever connecting.
        if summary["online"] == 0:
            raise DeviceOfflineError(summary)

        logger.info(f"Connecting to Addx WebRTC -> RTSP output: {RTSP_OUTPUT}")
        # Keep HA's battery/online sensors fresh on long streams: a session can
        # run for an hour+, and we'd otherwise publish state only once (above).
        # This re-uses `ticket` for a passive cloud re-read on a cadence, running
        # alongside the stream and cancelled when it ends.
        state_poller = None
        if mqtt is not None and SESSION_STATE_POLL_SECONDS > 0:
            state_poller = asyncio.create_task(_session_state_poller(mqtt, ticket))
        # Run the stream as a task so we can also watch for an `off` request that
        # arrives *mid-session*. The main loop only re-checks mode between
        # sessions, so without this a switch to `off` wouldn't take effect until
        # the stream happened to drop on its own (could be an hour+). Racing the
        # stream against mqtt.off_event() tears it down promptly instead.
        stream_task = asyncio.create_task(
            connect_and_stream(
                ticket=ticket,
                rtsp_output=RTSP_OUTPUT,
                a4x_user_id=a4x_user_id,
                serial_number=serial,
            )
        )
        waiters = [stream_task]
        off_task = None
        if mqtt is not None:
            off_task = asyncio.create_task(mqtt.off_event().wait())
            waiters.append(off_task)
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if off_task is not None and off_task.done() and not stream_task.done():
                logger.info("Mode=off requested mid-session — tearing down stream.")
                stream_task.cancel()
            # Surface any stream error/result (a clean end raises nothing; a
            # cancel from the off path is swallowed as an intentional teardown).
            with contextlib.suppress(asyncio.CancelledError):
                await stream_task
        finally:
            if off_task is not None and not off_task.done():
                off_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await off_task
            if state_poller is not None:
                state_poller.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await state_poller
            # Mirror the browser teardown so the cloud doesn't keep a stale
            # session pinned to our (now-dead) traceId. Best-effort.
            await stop_live(ticket)
    else:
        # Step 3b: KVS WebRTC path (not yet implemented)
        # The camera does not use Addx. It uses AWS Kinesis Video Streams.
        # To implement: call get_stream_play(auth_data, target, provider="KVS_WEBRTC")
        # then use boto3 + KVS Signaling client with the returned AWS credentials.
        logger.error(
            "Device is NOT an Addx device (onAddx=False). "
            "KVS WebRTC path is not yet implemented. "
            "Device details: " + str({k: target.get(k) for k in
                ['serialNumber', 'name', 'onAddx', 'region']})
        )
        raise RuntimeError(
            "KVS WebRTC (non-Addx) path not yet implemented. "
            "Only Addx devices (Birdfy Feeder Bamboo, Feeder, Cam) are currently supported."
        )


# A session that ran at least this long actually connected and streamed; anything
# shorter is a failed handshake or an offline/asleep camera. Failed handshakes can
# take ~50s (the camera waits before sending PEER_OUT), so the bar is 60s.
SESSION_OK_SECONDS = 60

# Backoff schedule for consecutive failed/short sessions. The previous loop reset
# to a fixed 2s on EVERY short session, so an offline camera was retried ~4x/min
# indefinitely — one ~9h outage produced ~2000 getWebrtcTicket wake-pokes and never
# escalated. Now each consecutive failure steps further along this ladder and holds
# at the 5-min cap, so an offline camera is polled every few minutes until it
# returns, then a successful stream resets us to the bottom. Each poke also nudges
# the cloud to wake a battery cam, so backing off protects the battery too.
BACKOFF_SCHEDULE = (2, 10, 30, 60, 120, 300)


# In `off` mode, refresh the device state (battery/online/awake/charging) this
# often so the HA sensors stay live instead of holding a stale retained value
# from the last stream. selectsingledevice is a passive cloud read (the state the
# camera last reported to the cloud — same read the live-session poller uses, see
# SESSION_STATE_POLL_SECONDS), so polling here costs no camera wake-poke. Default
# 600s (10 min). Set 0 to disable polling entirely and leave the last value frozen.
OFF_POLL_SECONDS = int(os.getenv("BIRDFY_OFF_POLL_SECONDS", "600") or "0")


# When the bridge *enters* `off` mode (e.g. the operator just flipped the HA
# select to off, or stopped a stream), the previously-published online/awake
# sensors are now stale — the camera may sleep moments later. Rather than wait a
# full OFF_POLL_SECONDS to correct them, do one poll shortly after entering off.
# A short delay (default 15s) lets the camera's cloud state settle after a
# just-ended session before we read it. 0 polls immediately on entry.
OFF_POLL_INITIAL_SECONDS = int(os.getenv("BIRDFY_OFF_POLL_INITIAL_SECONDS", "15") or "0")


# While a WebRTC session is live, re-read device state on this cadence so HA's
# battery/online sensors don't go stale on long streams. A single uninterrupted
# session can run for an hour+, during which we'd otherwise publish battery only
# once (at session start) — HA then shows a frozen value. This re-uses the
# already-issued addx ticket to call selectsingledevice, which is a passive cloud
# read (the state the camera last reported to the cloud), so it costs no extra
# camera wake-poke beyond the session we're already holding open. 0 disables.
SESSION_STATE_POLL_SECONDS = int(
    os.getenv("BIRDFY_SESSION_STATE_POLL_SECONDS", "60") or "0"
)


# Sentinel the container healthcheck watches to know the bridge is intentionally
# in `off` mode. In off mode the bridge never publishes to MediaMTX, so the
# healthcheck's publish check would otherwise flap UNHEALTHY after its grace
# window and make Docker restart a perfectly healthy container. We touch this file
# while off and remove it as soon as we leave off, so "not publishing" is
# distinguishable from "broken". Must match _OFF_SENTINEL in docker/healthcheck.py.
OFF_SENTINEL = os.getenv("BIRDFY_OFF_SENTINEL", "/tmp/birdfy_mode_off")


def _set_off_sentinel(active: bool) -> None:
    """Create/remove the off-mode healthcheck sentinel. Best-effort: a failure to
    touch it would only risk a spurious restart, never crash the bridge."""
    try:
        if active:
            with open(OFF_SENTINEL, "w") as f:
                f.write("off\n")
        else:
            try:
                os.unlink(OFF_SENTINEL)
            except FileNotFoundError:
                pass
    except OSError as e:  # noqa: BLE001
        logger.warning("Could not update off sentinel %s: %s", OFF_SENTINEL, e)


async def _publish_state_from_ticket(mqtt: MqttControl, ticket: dict) -> None:
    """Re-read device state with an existing ticket and publish to MQTT.

    Best-effort: logs and returns on any error so a transient cloud hiccup
    never tears down the live session this runs alongside.
    """
    state = await select_single_device(ticket)
    summary = device_state_summary(state)
    mqtt.publish_state(summary)
    batt = summary["battery_level"]
    logger.info(
        "[session] state poll: online=%s awake=%s battery=%s",
        summary["online"], summary["awake"],
        f"{batt}%" if batt is not None else "unknown",
    )


async def _session_state_poller(mqtt: MqttControl, ticket: dict) -> None:
    """Periodically refresh device state for the duration of a live session.

    Runs concurrently with connect_and_stream and is cancelled when the session
    ends. The first publish happens at session start (in run_once), so this only
    fires the *repeat* refreshes; hence we sleep before the first poll.
    """
    try:
        while True:
            await asyncio.sleep(SESSION_STATE_POLL_SECONDS)
            try:
                await _publish_state_from_ticket(mqtt, ticket)
            except Exception as e:  # noqa: BLE001
                logger.warning("[session] state poll failed: %s", e)
    except asyncio.CancelledError:
        raise


async def poll_state_only(mqtt: MqttControl) -> None:
    """Fetch device state without opening a WebRTC session, publish to MQTT.

    Used in `off` mode to keep HA's battery/online sensors fresh (when
    BIRDFY_OFF_POLL_SECONDS > 0) without the wake-poke of a full handshake.
    Best-effort: logs and returns on any error.
    """
    try:
        auth_data, devices = await login_or_resume(BIRDFY_EMAIL, BIRDFY_PASSWORD)
        target = _pick_device(devices)
        if target is None or not target.get("onAddx"):
            return
        mqtt.configure(str(target["serialNumber"]), target.get("deviceName") or "Birdfy")
        device_region = target.get("region") or auth_data.get("region")
        ticket = await get_addx_ticket(auth_data, device=target, device_region=device_region)
        state = await select_single_device(ticket)
        summary = device_state_summary(state)
        mqtt.publish_state(summary)
        batt = summary["battery_level"]
        logger.info(
            "[off] state poll: online=%s battery=%s",
            summary["online"], f"{batt}%" if batt is not None else "unknown",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[off] state poll failed: %s", e)


async def main():
    import time

    mqtt = MqttControl(MqttConfig())
    # NB: we deliberately do NOT start() here. The MQTT task computes every topic
    # (and the HA discovery node id) from the device serial, which isn't known
    # until the first device fetch. Starting now would publish discovery under
    # "birdfy_unknown" and a state topic of birdfy/unknown/state, while later
    # publish_state() calls go to birdfy/<real-sn>/state — so HA's sensors would
    # forever read Unknown. configure() (called once the serial is known) starts
    # the task with the correct serial bound.

    consecutive_failures = 0
    # Tracks whether the previous loop iteration was already in `off` mode, so the
    # first pass after *entering* off can do a prompt initial poll (correcting the
    # now-stale online/awake sensors) instead of waiting a full OFF_POLL_SECONDS.
    was_off = False
    # Each loop here is one camera session. The Birdfy's cloud WebRTC session
    # drops every few minutes (camera sleep/wake), so re-publishing the RTSP
    # output is normal, not an error. Downstream impact operators hit:
    # every drop EOFs Frigate's detect ffmpeg, and go2rtc keeps the `birdfy`
    # producer alive only while a consumer is attached — so during the gap
    # between EOF and Frigate's detect-ffmpeg restart, detect goes fully dark and
    # Frigate's record maintainer stalls ("Too many unprocessed recording
    # segments … keeping the N most recent"), silently dropping recordings. Fix
    # is downstream config, not here:
    #   - go2rtc >= 1.9.11: add a top-level `preload:` block for `birdfy` (and
    #     any derived substream) so go2rtc maintains the streams regardless of
    #     consumers. This is the recommended fix.
    #   - go2rtc <= 1.9.10 (Frigate 0.15 bundles 1.9.10): lower the feeder
    #     camera's ffmpeg `retry_interval` (default 10 -> 3) to shorten the gap.
    while True:
        # `off` mode: don't connect at all (no WebRTC wake-pokes). Optionally
        # refresh HA's battery/online sensors on a slow cadence, then wait. Touch
        # the off sentinel so the healthcheck knows "not publishing" is intentional
        # and doesn't restart us (see _set_off_sentinel).
        if mqtt.get_mode() == "off":
            _set_off_sentinel(True)
            if OFF_POLL_SECONDS > 0:
                # On the *first* pass after entering off, the published
                # online/awake/charging values are stale (left over from the last
                # stream) and the camera may go dormant any moment — so poll soon
                # after a short settle delay rather than waiting the full cadence.
                # Steady-state passes just poll on the OFF_POLL_SECONDS cadence.
                if not was_off and OFF_POLL_INITIAL_SECONDS > 0:
                    await asyncio.sleep(OFF_POLL_INITIAL_SECONDS)
                await poll_state_only(mqtt)
                delay = OFF_POLL_SECONDS
            else:
                # Polling disabled: leave the camera fully alone; sensors hold
                # their last value. Re-check the mode at the back-off cap.
                delay = BACKOFF_SCHEDULE[-1]
            was_off = True
            logger.info("Mode=off — bridge paused, re-checking mode in %ss ...", delay)
            await asyncio.sleep(delay)
            continue
        # Not off (or just left off): clear the sentinel so a genuinely stuck
        # stream can still go unhealthy, and reset the off-entry tracker so the
        # next switch into off triggers a fresh prompt poll.
        _set_off_sentinel(False)
        was_off = False

        t_start = time.monotonic()
        try:
            await run_once(mqtt)
            logger.warning("Session ended cleanly — reconnecting")
        except DeviceOfflineError as e:
            # Camera reported online=0 before we even tried the handshake.
            # Expected for a sleeping/charging battery cam — not an error, no
            # stack trace. Back off at the cap (no point polling every 2s) and
            # leave the failure ladder alone; we never opened a WebRTC session.
            delay = BACKOFF_SCHEDULE[-1]
            logger.info(
                "%s — skipping handshake, re-checking in %ss ...", e, delay
            )
            await asyncio.sleep(delay)
            continue
        except Exception as e:
            logger.error(f"Bridge error: {e}", exc_info=(log_level == "DEBUG"))

        elapsed = time.monotonic() - t_start
        if elapsed >= SESSION_OK_SECONDS:
            # Real session: the camera connected and streamed. Reset the ladder so
            # the next blip retries quickly.
            consecutive_failures = 0
            delay = BACKOFF_SCHEDULE[0]
            logger.info(f"Session lasted {elapsed:.0f}s — reconnecting in {delay}s ...")
        else:
            idx = min(consecutive_failures, len(BACKOFF_SCHEDULE) - 1)
            delay = BACKOFF_SCHEDULE[idx]
            consecutive_failures += 1
            if delay >= BACKOFF_SCHEDULE[-1]:
                logger.warning(
                    f"Camera unreachable ({consecutive_failures} consecutive short "
                    f"sessions) — backing off {delay}s (camera likely offline/asleep)."
                )
            else:
                logger.info(
                    f"Short session ({elapsed:.0f}s, failure #{consecutive_failures}) "
                    f"— retrying in {delay}s ..."
                )
        await asyncio.sleep(delay)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
