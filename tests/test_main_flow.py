"""Tests for main.py's session decision logic — the behavior changes that, if
broken, would silently stop the bridge from streaming or make it churn against an
offline camera.

Covered:
  * run_once() raises DeviceOfflineError when the camera reports online=0, and
    only on a *definite* 0 (never on unknown/missing) — the offline short-circuit.
  * run_once() proceeds to connect_and_stream when online=1, and publishes the
    state summary to MQTT.
  * poll_state_only() publishes state without ever opening a WebRTC session.

The infinite main() loop itself (sleep/backoff timing) is not unit-tested — it's
thin glue over these pieces; testing it would mean mocking time and is brittle.
"""
import os

import pytest

# main.py reads BIRDFY_EMAIL/PASSWORD into module-level constants at import time.
# Set them ONLY for the import, then restore the environment so we don't make the
# credentials-gated integration tests think real creds are present (which would
# turn their skip into a failing live login). main never re-reads os.environ
# after import, so removing them afterward is safe.
_saved_env = {k: os.environ.get(k) for k in ("BIRDFY_EMAIL", "BIRDFY_PASSWORD", "MQTT_HOST")}
os.environ["BIRDFY_EMAIL"] = "test@example.com"
os.environ["BIRDFY_PASSWORD"] = "pw"
os.environ.pop("MQTT_HOST", None)  # keep MQTT disabled for these tests

import main  # noqa: E402
from birdfy_api import DeviceOfflineError  # noqa: E402

for _k, _v in _saved_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

_DEVICE = {
    "serialNumber": "SN1",
    "deviceName": "Birdfy Feeder",
    "onAddx": True,
    "region": "us-east-1",
}


def _patch_common(monkeypatch, *, state):
    """Stub the API calls run_once/poll_state_only depend on.

    `state` is the dict select_single_device returns (the `data` payload).
    """
    async def fake_login(email, password):
        return ({"userID": 1, "region": "us-east-1"}, [_DEVICE])

    async def fake_ticket(auth, device=None, device_region=None, **kw):
        return {"_addx_sn": "SN1"}

    async def fake_select(ticket):
        return state

    monkeypatch.setattr(main, "login_or_resume", fake_login)
    monkeypatch.setattr(main, "get_addx_ticket", fake_ticket)
    monkeypatch.setattr(main, "select_single_device", fake_select)


class _SpyMqtt:
    """Stand-in for MqttControl that records publish_state calls."""

    def __init__(self):
        self.states = []

    def configure(self, *a, **k):
        pass

    def publish_state(self, summary):
        self.states.append(summary)

    def get_mode(self):
        return "auto"


# --- run_once offline short-circuit --------------------------------------

@pytest.mark.asyncio
async def test_run_once_raises_offline_when_online_zero(monkeypatch):
    _patch_common(monkeypatch, state={"online": 0, "batteryLevel": 5})

    # connect_and_stream must NOT be reached.
    reached = []

    async def fake_connect(**kw):
        reached.append(True)

    monkeypatch.setattr(main, "connect_and_stream", fake_connect)

    with pytest.raises(DeviceOfflineError):
        await main.run_once(_SpyMqtt())
    assert reached == []  # never attempted the handshake


@pytest.mark.asyncio
async def test_run_once_connects_when_online(monkeypatch):
    _patch_common(monkeypatch, state={"online": 1, "batteryLevel": 80})

    reached = []

    async def fake_connect(**kw):
        reached.append(True)

    async def fake_stop(ticket):
        pass

    monkeypatch.setattr(main, "connect_and_stream", fake_connect)
    monkeypatch.setattr(main, "stop_live", fake_stop)

    spy = _SpyMqtt()
    await main.run_once(spy)
    assert reached == [True]  # handshake attempted
    # State was published to MQTT before connecting.
    assert spy.states and spy.states[0]["online"] == 1
    assert spy.states[0]["battery_level"] == 80


@pytest.mark.asyncio
async def test_run_once_unknown_online_does_not_skip(monkeypatch):
    # online field absent -> summary online is None -> must NOT be treated as
    # offline (else an API shape change stops us ever connecting).
    _patch_common(monkeypatch, state={"batteryLevel": 50})

    reached = []

    async def fake_connect(**kw):
        reached.append(True)

    async def fake_stop(ticket):
        pass

    monkeypatch.setattr(main, "connect_and_stream", fake_connect)
    monkeypatch.setattr(main, "stop_live", fake_stop)

    await main.run_once(_SpyMqtt())
    assert reached == [True]  # connected despite unknown online state


# --- poll_state_only ------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_state_only_publishes_without_connecting(monkeypatch):
    _patch_common(monkeypatch, state={"online": 0, "batteryLevel": 12})

    # If poll_state_only ever called connect_and_stream, this would fire.
    async def boom(**kw):
        raise AssertionError("poll_state_only must not open a WebRTC session")

    monkeypatch.setattr(main, "connect_and_stream", boom)

    spy = _SpyMqtt()
    await main.poll_state_only(spy)
    assert spy.states and spy.states[0]["battery_level"] == 12


@pytest.mark.asyncio
async def test_poll_state_only_swallows_errors(monkeypatch):
    async def boom_login(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(main, "login_or_resume", boom_login)
    # Must not raise — best-effort.
    await main.poll_state_only(_SpyMqtt())
