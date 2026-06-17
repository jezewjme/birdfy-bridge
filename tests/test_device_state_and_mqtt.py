"""Tests for device-state parsing and the optional MQTT control layer.

The state fields (online/awake/batteryLevel/isCharging) were confirmed against a
live Birdfy Feeder Bamboo's selectsingledevice response. The offline short-circuit
and the MQTT-disabled no-op paths are the safety-critical bits: a regression in
device_state_summary could make the bridge either churn against an offline cam or
refuse to ever connect, and a regression in the MQTT guard could take down
streaming when no broker is configured.
"""
import asyncio
import json

import pytest

from birdfy_api import DeviceOfflineError, device_state_summary
from mqtt_control import MODES, MqttConfig, MqttControl

# --- device_state_summary -------------------------------------------------

def test_summary_parses_live_fields():
    # Shape from a real selectsingledevice `data` payload.
    state = {
        "online": 1, "awake": 0, "batteryLevel": 42,
        "isCharging": 1, "offlineTime": None, "irrelevant": "x",
    }
    s = device_state_summary(state)
    assert s == {
        "online": 1, "awake": 0, "battery_level": 42,
        "is_charging": 1, "offline_time": None,
    }


def test_summary_coerces_strings_and_handles_missing():
    s = device_state_summary({"online": "0", "batteryLevel": "7"})
    assert s["online"] == 0
    assert s["battery_level"] == 7
    # Missing fields -> None (so callers can tell "offline" from "unknown").
    assert s["awake"] is None
    assert s["is_charging"] is None


def test_summary_none_and_garbage_are_safe():
    assert device_state_summary(None)["online"] is None
    s = device_state_summary({"online": "notanint"})
    assert s["online"] is None  # unparseable -> None, never raises


def test_offline_only_on_definite_zero():
    # online == 0 -> offline; None (unknown) must NOT be treated as offline,
    # else an API shape change would stop us ever connecting.
    assert device_state_summary({"online": 0})["online"] == 0
    assert device_state_summary({})["online"] is None


def test_device_offline_error_message_includes_battery():
    e = DeviceOfflineError({"battery_level": 3})
    assert "3%" in str(e)
    e2 = DeviceOfflineError({"battery_level": None})
    assert "unknown" in str(e2)


# --- MqttControl disabled path -------------------------------------------

def test_mqtt_disabled_when_no_host(monkeypatch):
    monkeypatch.delenv("MQTT_HOST", raising=False)
    cfg = MqttConfig()
    assert cfg.enabled is False
    m = MqttControl(cfg)
    # All public calls must be safe no-ops with no broker.
    m.start()
    m.publish_state({"battery_level": 50, "online": 1})
    assert m.get_mode() in MODES


def test_mqtt_default_mode_env(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "broker.local")
    monkeypatch.setenv("BIRDFY_MODE", "off")
    assert MqttConfig().default_mode == "off"
    # Invalid mode falls back to the safe default.
    monkeypatch.setenv("BIRDFY_MODE", "bogus")
    assert MqttConfig().default_mode == "auto"


# --- start()/configure() serial-binding ordering --------------------------

@pytest.mark.asyncio
async def test_start_is_inert_until_configured(monkeypatch):
    # Regression: main() used to start() before the serial was known, so the
    # task published HA discovery under "birdfy_unknown" and state under
    # birdfy/unknown/state, while publish_state() later wrote to the real-serial
    # topic — HA's sensors read Unknown forever. start() must refuse to launch
    # the task until configure() binds a serial.
    monkeypatch.setenv("MQTT_HOST", "broker.local")
    m = MqttControl(MqttConfig())  # constructed with no serial
    m.start()
    assert m._task is None, "start() must not launch the task before configure()"

    # configure() binds the real serial, updates the node, and starts the task.
    m.configure("SN_REAL", "Birdfy Feeder")
    assert m._sn == "SN_REAL"
    assert m._node == "birdfy_SN_REAL"
    assert m._task is not None
    # Clean up the task we just started so it doesn't leak into other tests.
    m._task.cancel()


@pytest.mark.asyncio
async def test_configured_node_drives_discovery_and_state_topics(monkeypatch):
    # After configure() the discovery node id and the state topic must both use
    # the real serial — never the "unknown" placeholder.
    monkeypatch.setenv("MQTT_HOST", "broker.local")
    m = MqttControl(MqttConfig())
    m.configure("SN_REAL", "Birdfy Feeder")
    m._task.cancel()  # we only want the topic binding, not the live task

    client = _FakeClient()
    await m._publish_discovery(client)
    for topic, _payload, _retain in client.published:
        assert "unknown" not in topic
        assert "SN_REAL" in topic

    client2 = _FakeClient()
    await m._publish_state_payload(client2, {"battery_level": 57})
    topic, _payload, _retain = client2.published[0]
    assert topic == "birdfy/SN_REAL/state"


# --- MqttControl publishing (against a fake client) -----------------------

class _FakeClient:
    def __init__(self):
        self.published = []
        self.subscribed = []

    async def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    async def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)


@pytest.mark.asyncio
async def test_discovery_payloads_are_valid_json_with_unique_ids(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "broker.local")
    m = MqttControl(MqttConfig(), serial="SN123", device_name="Birdfy Feeder")
    client = _FakeClient()
    await m._publish_discovery(client)
    # One select + battery sensor + three binary sensors.
    assert len(client.published) == 5
    uids = set()
    for topic, payload, retain in client.published:
        assert retain is True
        obj = json.loads(payload)  # raises on invalid JSON
        assert obj["unique_id"] not in uids  # uniqueness
        uids.add(obj["unique_id"])
        assert "SN123" in topic


@pytest.mark.asyncio
async def test_apply_mode_rejects_invalid_and_accepts_valid(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "broker.local")
    m = MqttControl(MqttConfig(), serial="SN1")
    client = _FakeClient()
    await m._apply_mode(client, b"garbage")
    assert m.get_mode() == "auto"  # unchanged
    await m._apply_mode(client, b"off")
    assert m.get_mode() == "off"
    # Echoed to the retained state topic.
    assert any("mode/state" in t and p == "off" and r for t, p, r in client.published)


@pytest.mark.asyncio
async def test_state_payload_includes_mode_and_fields(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "broker.local")
    m = MqttControl(MqttConfig(), serial="SN1")
    m._mode = "always_on"
    client = _FakeClient()
    await m._publish_state_payload(client, device_state_summary(
        {"online": 1, "awake": 1, "batteryLevel": 88, "isCharging": 0}
    ))
    topic, payload, retain = client.published[0]
    obj = json.loads(payload)
    assert obj["battery_level"] == 88
    assert obj["mode"] == "always_on"
    assert retain is True


# --- publish_state queue behavior ----------------------------------------

def test_publish_state_coalesces_when_queue_full(monkeypatch):
    # When the outgoing queue is full, the freshest reading must win — a stale
    # battery value should never linger ahead of a newer one.
    monkeypatch.setenv("MQTT_HOST", "broker.local")
    m = MqttControl(MqttConfig(), serial="SN1")
    # Fill the queue to capacity with old readings.
    for i in range(m._state_q.maxsize):
        m.publish_state({"battery_level": i})
    assert m._state_q.full()
    # One more — must not raise, and the newest value must be retrievable.
    m.publish_state({"battery_level": 999})
    drained = []
    while not m._state_q.empty():
        drained.append(m._state_q.get_nowait()["battery_level"])
    assert 999 in drained
    # _last_state always reflects the most recent call regardless of queue state.
    assert m._last_state["battery_level"] == 999


@pytest.mark.asyncio
async def test_run_retries_on_connection_failure_without_raising(monkeypatch):
    # _run must survive a broker that refuses connections: log + backoff + retry,
    # never propagate (MQTT must never take down the bridge). We inject a fake
    # aiomqtt whose Client() raises, and a sleep that cancels after a few tries.
    import sys
    import types

    attempts = {"n": 0}

    class _FakeClient:
        def __init__(self, **kw):
            attempts["n"] += 1
            raise OSError("connection refused")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    fake_aiomqtt = types.ModuleType("aiomqtt")
    fake_aiomqtt.Client = _FakeClient
    fake_aiomqtt.Will = lambda **kw: None
    monkeypatch.setitem(sys.modules, "aiomqtt", fake_aiomqtt)

    # Break out of the infinite retry loop after 3 attempts via the sleep hook.
    import mqtt_control

    async def fake_sleep(_):
        if attempts["n"] >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(mqtt_control.asyncio, "sleep", fake_sleep)

    m = MqttControl(MqttConfig(), serial="SN1")
    with pytest.raises(asyncio.CancelledError):
        await m._run()
    assert attempts["n"] >= 3  # it kept retrying, didn't give up or raise out
