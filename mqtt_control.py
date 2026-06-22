"""
Optional MQTT control + observability for the Birdfy bridge.

This layer is *additive and optional*: if MQTT_HOST is unset, none of this runs
and the bridge behaves exactly as it did before (see main.py). It never sits in
the media path, and MQTT failures are logged but never propagate into streaming.

What it provides when a broker is configured:

  * A 3-way **mode** the operator controls from Home Assistant:
      - always_on : connect whenever the camera is online (max coverage).
      - auto      : connect, but defer to the camera's *native* dormancy schedule
                    (set in the Birdfy app). During the overnight window the
                    camera reports online=0 and main()'s offline back-off kicks
                    in — no special code here beyond honoring online=0.
      - off       : pause the connect loop entirely (no WebRTC wake-pokes). The
                    camera is left alone to conserve battery.
    The chosen mode is persisted to a file in BIRDFY_STATE_DIR (.birdfy_mode) so it
    survives a container restart, and is also echoed to a *retained* MQTT topic so
    HA's select reflects it. The persisted file is the source of truth at boot;
    BIRDFY_MODE only seeds the first-ever boot before that file exists.

  * **Sensors** published to MQTT (and auto-created in HA via MQTT Discovery):
    Battery %, Online, Awake, Charging, plus the current Mode. Battery is the
    one that matters most — alert on it in HA and you'll never get blindsided by
    a dead camera again.

Topic layout (base defaults to "birdfy", node id is the device serial):
  <base>/<sn>/mode/set     (retained, HA -> bridge)  mode command
  <base>/<sn>/mode/state   (retained, bridge -> HA)  current mode
  <base>/<sn>/state        (retained, bridge -> HA)  JSON: battery/online/awake/...
  <base>/<sn>/availability  (bridge -> HA)           "online"/"offline" (LWT)
  <discovery_prefix>/{select,binary_sensor,sensor}/birdfy_<sn>/<obj>/config

Design notes:
  * aiomqtt 2.x async API. We run one long-lived task that owns the client,
    reconnects on drop, and serves both directions (subscribe to mode/set,
    publish state on demand via an asyncio.Queue the bridge pushes to).
  * The bridge calls publish_state(summary) after each selectsingledevice; we
    fan that out to MQTT. get_mode() returns the latest mode for the loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib

logger = logging.getLogger("mqtt_control")

# Valid modes, in the order HA's select should present them.
MODES = ("always_on", "auto", "off")
DEFAULT_MODE = "auto"

# Where the chosen mode is persisted so it survives a container restart. Same
# BIRDFY_STATE_DIR the auth token / UDID use (mounted as a Docker volume in
# compose), defaulting to the home dir for parity with birdfy_api.py. The file is
# the source of truth at boot; BIRDFY_MODE only seeds the *first-ever* boot before
# this file exists. We resolve the dir the same way birdfy_api does so a single
# BIRDFY_STATE_DIR covers every persisted artifact.
try:
    _STATE_DIR = pathlib.Path(os.getenv("BIRDFY_STATE_DIR", str(pathlib.Path.home())))
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    _STATE_DIR = pathlib.Path.home()
_MODE_FILE = _STATE_DIR / ".birdfy_mode"


def _read_persisted_mode() -> str | None:
    """Return the persisted mode if a valid one was saved, else None.

    Best-effort: a missing file, unreadable dir, or garbage contents all return
    None so the caller falls back to the env default — persistence must never
    block the bridge from starting.
    """
    try:
        mode = _MODE_FILE.read_text().strip().lower()
    except OSError:
        return None
    return mode if mode in MODES else None


def _write_persisted_mode(mode: str) -> None:
    """Persist the mode for the next restart. Best-effort (logs, never raises)."""
    if mode not in MODES:
        return
    try:
        _MODE_FILE.write_text(mode + "\n")
    except OSError as e:  # noqa: BLE001
        logger.warning("Could not persist mode to %s: %s", _MODE_FILE, e)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


class MqttConfig:
    """Resolved MQTT configuration. `enabled` is False when no host is set."""

    def __init__(self) -> None:
        self.host = _env("MQTT_HOST").strip()
        self.enabled = bool(self.host)
        self.port = int(_env("MQTT_PORT", "1883") or "1883")
        self.username = _env("MQTT_USERNAME").strip() or None
        self.password = _env("MQTT_PASSWORD") or None
        self.base_topic = (_env("MQTT_BASE_TOPIC", "birdfy").strip() or "birdfy").rstrip("/")
        self.discovery_prefix = (
            _env("MQTT_DISCOVERY_PREFIX", "homeassistant").strip() or "homeassistant"
        ).rstrip("/")
        # Default mode at first boot (before HA publishes a retained mode).
        default_mode = _env("BIRDFY_MODE", DEFAULT_MODE).strip().lower()
        self.default_mode = default_mode if default_mode in MODES else DEFAULT_MODE


class MqttControl:
    """Owns the MQTT client lifecycle and bridges state both directions.

    Safe to construct unconditionally; if `config.enabled` is False, `start()`
    is a no-op and `get_mode()` always returns the env-var default so the bridge
    runs normally without a broker.
    """

    def __init__(self, config: MqttConfig, serial: str = "", device_name: str = "Birdfy") -> None:
        self._cfg = config
        self._sn = serial or "unknown"
        self._device_name = device_name or "Birdfy"
        self._configured = bool(serial)
        # Persisted mode (from a prior run) wins over the env default so the
        # operator's last choice survives a container restart; BIRDFY_MODE only
        # applies on the first-ever boot before .birdfy_mode exists.
        persisted = _read_persisted_mode()
        if persisted is not None:
            logger.info("Restored persisted mode %r (env default was %r)", persisted, config.default_mode)
        self._mode = persisted if persisted is not None else config.default_mode
        # Latest state summary to (re)publish on reconnect, so HA isn't blank
        # after a broker restart.
        self._last_state: dict = {}
        self._state_q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._task: asyncio.Task | None = None
        self._node = f"birdfy_{self._sn}"
        # Set whenever the current mode is `off`, cleared otherwise. Lets a live
        # session in main() react to an off request *mid-stream* (await this
        # event) instead of only noticing at the top of the next loop iteration.
        # Created lazily on first access so MqttControl can be built off-loop.
        self._off_event: asyncio.Event | None = None
        # Edge-triggered: set on *any* real mode change so the bridge loop can cut
        # short whatever back-off / off-mode pause it's sitting in and re-evaluate
        # immediately. Without it a mode flip only takes effect when the current
        # asyncio.sleep happens to end — up to OFF_POLL_SECONDS (10 min) of lag.
        # The loop clears it after waking (see sleep_until_mode_change).
        self._mode_changed: asyncio.Event | None = None

    # ---- topic helpers ----
    @property
    def _t_base(self) -> str:
        return f"{self._cfg.base_topic}/{self._sn}"

    @property
    def _t_mode_set(self) -> str:
        return f"{self._t_base}/mode/set"

    @property
    def _t_mode_state(self) -> str:
        return f"{self._t_base}/mode/state"

    @property
    def _t_state(self) -> str:
        return f"{self._t_base}/state"

    @property
    def _t_avail(self) -> str:
        return f"{self._t_base}/availability"

    # ---- public API used by main() ----
    def get_mode(self) -> str:
        """Current mode. Always valid; defaults to env default if no broker."""
        return self._mode

    def off_event(self) -> asyncio.Event:
        """Event that is set while mode == `off`, cleared otherwise.

        A live session can `await mqtt.off_event().wait()` to be released the
        instant the user switches to `off`, rather than running until the stream
        happens to drop. Created on first access (needs a running loop), seeded
        from the current mode.
        """
        if self._off_event is None:
            self._off_event = asyncio.Event()
            if self._mode == "off":
                self._off_event.set()
        return self._off_event

    def _mode_changed_event(self) -> asyncio.Event:
        """Internal accessor for the edge-triggered mode-change event (lazy)."""
        if self._mode_changed is None:
            self._mode_changed = asyncio.Event()
        return self._mode_changed

    async def sleep_until_mode_change(self, delay: float) -> bool:
        """Sleep up to `delay` seconds, returning early on any mode change.

        The bridge loop uses this instead of a bare asyncio.sleep so that flipping
        the mode (off -> auto, auto -> off mid-pause, etc.) wakes it promptly
        instead of waiting out a full back-off / off-mode cadence. Returns True if
        a mode change interrupted the wait, False if the full delay elapsed. The
        change event is cleared before returning so the next call starts fresh.
        """
        changed = self._mode_changed_event()
        # Drain any change that fired while we weren't waiting so this call
        # reflects only transitions during the sleep itself.
        changed.clear()
        if delay <= 0:
            return False
        try:
            await asyncio.wait_for(changed.wait(), timeout=delay)
            interrupted = True
        except asyncio.TimeoutError:
            interrupted = False
        changed.clear()
        return interrupted

    def configure(self, serial: str, device_name: str = "") -> None:
        """Bind the real device serial/name and start the task (idempotent).

        The serial may not be known until after the first device fetch (when
        DEVICE_ID is unset and we fall back to the first device), so main() calls
        this once it has identified the device. Safe to call repeatedly; only the
        first call with a serial takes effect.
        """
        if self._configured:
            return
        if serial:
            self._sn = serial
            self._node = f"birdfy_{self._sn}"
        if device_name:
            self._device_name = device_name
        self._configured = True
        self.start()

    def start(self) -> None:
        """Launch the background MQTT task once the serial is bound.

        No-op when MQTT is disabled, and — importantly — a no-op until configure()
        has bound a real serial. Every topic and the HA discovery node id derive
        from the serial; starting before it's known would publish discovery under
        "birdfy_unknown" / state under birdfy/unknown/state, permanently mismatched
        from the real-serial topics publish_state() later writes to (HA reads
        Unknown forever). configure() is the intended launcher.
        """
        if not self._cfg.enabled:
            logger.info("MQTT disabled (MQTT_HOST unset) — running in mode=%s", self._mode)
            return
        if not self._configured:
            logger.debug("MQTT start() deferred — serial not bound yet")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    def publish_state(self, summary: dict) -> None:
        """Hand a device-state summary to the MQTT task for publishing.

        Called by the bridge after each selectsingledevice. Cheap and
        non-blocking: drops the update if the queue is full (next one wins) and
        is a no-op when MQTT is disabled. `summary` is device_state_summary()'s
        output (online/awake/battery_level/is_charging/offline_time).
        """
        self._last_state = dict(summary)
        if not self._cfg.enabled:
            return
        try:
            self._state_q.put_nowait(dict(summary))
        except asyncio.QueueFull:
            # Coalesce: clear one and push the freshest.
            try:
                self._state_q.get_nowait()
                self._state_q.put_nowait(dict(summary))
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    # ---- internals ----
    async def _run(self) -> None:
        """Long-lived client loop with reconnect. Never raises out."""
        import aiomqtt  # imported lazily so the dep is only needed when enabled

        backoff = 2
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self._cfg.host,
                    port=self._cfg.port,
                    username=self._cfg.username,
                    password=self._cfg.password,
                    will=aiomqtt.Will(
                        topic=self._t_avail, payload="offline", qos=1, retain=True
                    ),
                ) as client:
                    logger.info(
                        "MQTT connected to %s:%s (base=%s)",
                        self._cfg.host, self._cfg.port, self._cfg.base_topic,
                    )
                    backoff = 2
                    await self._on_connect(client)
                    await self._serve(client)
            except Exception as e:  # noqa: BLE001 - never let MQTT kill the bridge
                logger.warning("MQTT connection lost: %s — retrying in %ss", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _on_connect(self, client) -> None:
        # Announce availability, publish discovery, restore retained state,
        # then subscribe to mode commands.
        await client.publish(self._t_avail, "online", qos=1, retain=True)
        await self._publish_discovery(client)
        await client.publish(self._t_mode_state, self._mode, qos=1, retain=True)
        if self._last_state:
            await self._publish_state_payload(client, self._last_state)
        await client.subscribe(self._t_mode_set, qos=1)

    async def _serve(self, client) -> None:
        """Concurrently pump outgoing state and handle incoming mode commands."""
        async def _pump_state() -> None:
            while True:
                summary = await self._state_q.get()
                await self._publish_state_payload(client, summary)

        async def _handle_messages() -> None:
            async for message in client.messages:
                if message.topic.matches(self._t_mode_set):
                    await self._apply_mode(client, message.payload)

        await asyncio.gather(_pump_state(), _handle_messages())

    async def _apply_mode(self, client, payload) -> None:
        try:
            mode = payload.decode().strip().lower()
        except Exception:  # noqa: BLE001
            mode = ""
        if mode not in MODES:
            logger.warning("MQTT: ignoring invalid mode %r (valid: %s)", mode, MODES)
            return
        if mode != self._mode:
            logger.info("MQTT: mode %s -> %s", self._mode, mode)
            # Persist the new choice so it survives a container restart. Only on a
            # real change to avoid rewriting the file on every retained-mode echo.
            _write_persisted_mode(mode)
            # Wake the bridge loop out of any back-off / off-mode pause so it
            # re-evaluates against the new mode now, not at the next sleep edge.
            if self._mode_changed is not None:
                self._mode_changed.set()
        self._mode = mode
        # Release/re-arm any live session waiting on the off event.
        if self._off_event is not None:
            if mode == "off":
                self._off_event.set()
            else:
                self._off_event.clear()
        # Echo to the state topic (retained) so HA's select reflects the change.
        await client.publish(self._t_mode_state, mode, qos=1, retain=True)

    async def _publish_state_payload(self, client, summary: dict) -> None:
        payload = json.dumps(
            {
                "battery_level": summary.get("battery_level"),
                "online": summary.get("online"),
                "awake": summary.get("awake"),
                "is_charging": summary.get("is_charging"),
                "offline_time": summary.get("offline_time"),
                "mode": self._mode,
            }
        )
        await client.publish(self._t_state, payload, qos=1, retain=True)

    async def _publish_discovery(self, client) -> None:
        """Publish HA MQTT-discovery configs so the device + entities auto-appear."""
        dev = {
            "identifiers": [self._node],
            "name": self._device_name,
            "manufacturer": "Birdfy / Netvue",
            "model": "Addx WebRTC feeder",
        }
        avail = [{"topic": self._t_avail}]
        prefix = self._cfg.discovery_prefix

        def cfg_topic(component: str, obj: str) -> str:
            return f"{prefix}/{component}/{self._node}/{obj}/config"

        # Mode select.
        await client.publish(
            cfg_topic("select", "mode"),
            json.dumps({
                "name": "Mode",
                "unique_id": f"{self._node}_mode",
                "command_topic": self._t_mode_set,
                "state_topic": self._t_mode_state,
                "options": list(MODES),
                "availability": avail,
                "device": dev,
                "icon": "mdi:bird",
            }),
            qos=1, retain=True,
        )

        # Battery sensor (the important one).
        await client.publish(
            cfg_topic("sensor", "battery"),
            json.dumps({
                "name": "Battery",
                "unique_id": f"{self._node}_battery",
                "state_topic": self._t_state,
                "value_template": "{{ value_json.battery_level }}",
                "device_class": "battery",
                "unit_of_measurement": "%",
                "state_class": "measurement",
                "availability": avail,
                "device": dev,
            }),
            qos=1, retain=True,
        )

        # Binary sensors: online / awake / charging.
        for obj, name, dclass, key, icon in (
            ("online", "Online", "connectivity", "online", None),
            ("awake", "Awake", None, "awake", "mdi:sleep"),
            ("charging", "Charging", "battery_charging", "is_charging", None),
        ):
            payload = {
                "name": name,
                "unique_id": f"{self._node}_{obj}",
                "state_topic": self._t_state,
                # state field is 1/0; map to HA's ON/OFF. The {{ }} are Jinja
                # (HA-side), so build the template by concatenation rather than
                # an f-string to avoid escaping every brace.
                "value_template": "{{ 'ON' if value_json." + key + " == 1 else 'OFF' }}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "availability": avail,
                "device": dev,
            }
            if dclass:
                payload["device_class"] = dclass
            if icon:
                payload["icon"] = icon
            await client.publish(cfg_topic("binary_sensor", obj), json.dumps(payload), qos=1, retain=True)

        logger.info("MQTT: published HA discovery for device %s", self._node)
