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
import asyncio
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


@pytest.fixture(autouse=True)
def _isolate_off_sentinel(monkeypatch, tmp_path):
    """Redirect the off-mode healthcheck sentinel to a per-test temp path so the
    main() loop's _set_off_sentinel calls never touch the real /tmp default. Tests
    that assert on the sentinel re-point it to their own tmp_path file."""
    monkeypatch.setattr(main, "OFF_SENTINEL", str(tmp_path / "off_sentinel_default"))


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
        self._off = asyncio.Event()

    def configure(self, *a, **k):
        pass

    def publish_state(self, summary):
        self.states.append(summary)

    def get_mode(self):
        return "auto"

    def off_event(self):
        return self._off


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


@pytest.mark.asyncio
async def test_run_once_off_event_tears_down_live_session(monkeypatch):
    # A switch to `off` mid-stream must cancel a still-running connect_and_stream
    # (the bug: off only took effect between sessions, so a long stream ignored
    # it for an hour+). stop_live must still run on the way out.
    _patch_common(monkeypatch, state={"online": 1, "batteryLevel": 80})
    monkeypatch.setattr(main, "SESSION_STATE_POLL_SECONDS", 0)

    cancelled = []
    stopped = []

    async def never_ending_stream(**kw):
        try:
            await asyncio.Event().wait()  # blocks until cancelled
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def fake_stop(ticket):
        stopped.append(True)

    monkeypatch.setattr(main, "connect_and_stream", never_ending_stream)
    monkeypatch.setattr(main, "stop_live", fake_stop)

    spy = _SpyMqtt()

    async def flip_off_soon():
        # Let run_once reach the wait(), then request off.
        await asyncio.sleep(0.05)
        spy.off_event().set()

    await asyncio.gather(main.run_once(spy), flip_off_soon())

    assert cancelled == [True]  # stream was torn down by the off request
    assert stopped == [True]    # teardown still ran


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


# --- main() loop branches -------------------------------------------------
#
# main() is `while True`; we drive a bounded number of iterations by patching
# asyncio.sleep to raise CancelledError after the branch under test has run, then
# assert on what the branch did. This locks the off-mode pause and the
# DeviceOfflineError back-off path without a real (infinite) loop.


class _ModeMqtt:
    """MqttControl stand-in returning a fixed mode; records publish_state."""

    def __init__(self, mode):
        self._mode = mode
        self.states = []
        self._off = asyncio.Event()
        if mode == "off":
            self._off.set()

    def configure(self, *a, **k):
        pass

    def publish_state(self, summary):
        self.states.append(summary)

    def get_mode(self):
        return self._mode

    def off_event(self):
        return self._off


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 600),   # unset -> default
        ("", 600),     # set-but-empty (blank Docker/compose option) -> default, NOT 0
        ("   ", 600),  # whitespace-only -> default
        ("0", 0),      # explicit 0 -> disabled (honored)
        ("300", 300),  # explicit value
    ],
)
def test_env_int_treats_empty_as_default(monkeypatch, raw, expected):
    # Regression: the old `int(getenv(name, "600") or "0")` idiom turned a
    # blank-but-present env var into 0, silently disabling off-mode polling.
    if raw is None:
        monkeypatch.delenv("BIRDFY_OFF_POLL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("BIRDFY_OFF_POLL_SECONDS", raw)
    assert main._env_int("BIRDFY_OFF_POLL_SECONDS", 600) == expected


@pytest.mark.asyncio
async def test_main_off_mode_pauses_without_running_session(monkeypatch, tmp_path):
    # mode=off must NOT call run_once at all — it only waits and re-checks.
    monkeypatch.setattr(main, "MqttControl", lambda cfg: _ModeMqtt("off"))
    monkeypatch.setattr(main, "MqttConfig", lambda: object())
    monkeypatch.setattr(main, "OFF_POLL_SECONDS", 0)  # cadence polling disabled
    monkeypatch.setattr(main, "OFF_POLL_INITIAL_SECONDS", 0)
    # Point the off sentinel at a temp file so we can assert the healthcheck hint
    # is written while paused (its presence keeps the container HEALTHY in off mode).
    sentinel = tmp_path / "birdfy_mode_off"
    monkeypatch.setattr(main, "OFF_SENTINEL", str(sentinel))

    ran = []

    async def fake_run_once(mqtt):
        ran.append(True)

    monkeypatch.setattr(main, "run_once", fake_run_once)

    polled = []

    async def fake_poll(mqtt):
        polled.append(mqtt)
        return True  # published -> entry handled

    monkeypatch.setattr(main, "poll_state_only", fake_poll)

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        raise asyncio.CancelledError  # break the loop after the first off-pause

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main.main()
    assert ran == []  # never opened a session
    # Even with cadence polling disabled, the *entry* poll still runs once so
    # MQTT discovery/availability come up and HA isn't left showing `unavailable`.
    assert len(polled) == 1
    # Then paused at the back-off cap (OFF_POLL_SECONDS=0 -> no poll cadence).
    assert sleeps == [main.BACKOFF_SCHEDULE[-1]]
    # Off sentinel was created so the healthcheck treats not-publishing as
    # intentional rather than restarting the container.
    assert sentinel.exists()


@pytest.mark.asyncio
async def test_main_off_mode_polls_when_configured(monkeypatch, tmp_path):
    # OFF_POLL_SECONDS > 0 -> refresh sensors via poll_state_only, sleep that long.
    monkeypatch.setattr(main, "MqttControl", lambda cfg: _ModeMqtt("off"))
    monkeypatch.setattr(main, "MqttConfig", lambda: object())
    monkeypatch.setattr(main, "OFF_POLL_SECONDS", 1200)
    # Initial=0 -> poll immediately on entry (no extra settle sleep), so this test
    # asserts only the steady-cadence sleep. The initial-delay path is covered separately.
    monkeypatch.setattr(main, "OFF_POLL_INITIAL_SECONDS", 0)
    monkeypatch.setattr(main, "OFF_SENTINEL", str(tmp_path / "birdfy_mode_off"))

    polled = []

    async def fake_poll(mqtt):
        polled.append(mqtt)
        return True

    monkeypatch.setattr(main, "poll_state_only", fake_poll)

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main.main()
    assert len(polled) == 1
    assert sleeps == [1200]


@pytest.mark.asyncio
async def test_main_off_mode_initial_poll_uses_short_delay(monkeypatch, tmp_path):
    # On *entering* off, the first pass should wait OFF_POLL_INITIAL_SECONDS, poll
    # once to correct now-stale sensors, then sleep the steady OFF_POLL_SECONDS.
    monkeypatch.setattr(main, "MqttControl", lambda cfg: _ModeMqtt("off"))
    monkeypatch.setattr(main, "MqttConfig", lambda: object())
    monkeypatch.setattr(main, "OFF_POLL_SECONDS", 1200)
    monkeypatch.setattr(main, "OFF_POLL_INITIAL_SECONDS", 15)
    monkeypatch.setattr(main, "OFF_SENTINEL", str(tmp_path / "birdfy_mode_off"))

    events = []  # interleave sleeps and polls to assert ordering

    async def fake_poll(mqtt):
        events.append(("poll", None))
        return True

    monkeypatch.setattr(main, "poll_state_only", fake_poll)

    async def fake_sleep(delay):
        events.append(("sleep", delay))
        # Stop after the steady-cadence sleep (the 2nd sleep on the first off pass).
        if delay == 1200:
            raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main.main()
    # Settle delay -> poll -> steady cadence.
    assert events == [("sleep", 15), ("poll", None), ("sleep", 1200)]


@pytest.mark.asyncio
async def test_main_off_mode_entry_poll_retries_on_failure(monkeypatch, tmp_path):
    # If the entry poll can't publish (boot-time cloud/network hiccup), the loop
    # must NOT latch as handled — otherwise HA stays `unavailable` forever when
    # cadence polling is disabled. It should back off briefly and retry the entry
    # poll, then latch once it succeeds.
    monkeypatch.setattr(main, "MqttControl", lambda cfg: _ModeMqtt("off"))
    monkeypatch.setattr(main, "MqttConfig", lambda: object())
    monkeypatch.setattr(main, "OFF_POLL_SECONDS", 0)  # cadence disabled
    monkeypatch.setattr(main, "OFF_POLL_INITIAL_SECONDS", 0)
    monkeypatch.setattr(main, "OFF_SENTINEL", str(tmp_path / "birdfy_mode_off"))

    results = iter([False, True])  # first poll fails, second succeeds

    polls = []

    async def fake_poll(mqtt):
        ok = next(results)
        polls.append(ok)
        return ok

    monkeypatch.setattr(main, "poll_state_only", fake_poll)

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        # Break after the steady-state cap sleep that follows a successful entry.
        if delay == main.BACKOFF_SCHEDULE[-1]:
            raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main.main()
    # Failed poll -> short retry back-off; successful poll -> settle at the cap.
    assert polls == [False, True]
    assert sleeps == [main.BACKOFF_SCHEDULE[0], main.BACKOFF_SCHEDULE[-1]]


@pytest.mark.asyncio
async def test_main_offline_error_backs_off_at_cap(monkeypatch, tmp_path):
    # run_once raising DeviceOfflineError must route to the cap back-off and
    # continue (not crash, not count toward the failure ladder's short-session
    # branch).
    monkeypatch.setattr(main, "MqttControl", lambda cfg: _ModeMqtt("auto"))
    monkeypatch.setattr(main, "MqttConfig", lambda: object())
    # A stale off sentinel left from a previous off period must be cleared the
    # moment we re-enter a non-off iteration, so a genuinely stuck stream can still
    # go unhealthy.
    sentinel = tmp_path / "birdfy_mode_off"
    sentinel.write_text("off\n")
    monkeypatch.setattr(main, "OFF_SENTINEL", str(sentinel))

    async def fake_run_once(mqtt):
        raise DeviceOfflineError({"battery_level": 4})

    monkeypatch.setattr(main, "run_once", fake_run_once)

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main.main()
    assert sleeps == [main.BACKOFF_SCHEDULE[-1]]
    assert not sentinel.exists()  # cleared on the non-off iteration


def test_set_off_sentinel_create_and_remove(monkeypatch, tmp_path):
    # Direct unit test of the sentinel helper: idempotent create, idempotent
    # remove, and a remove of an absent file is a no-op (not an error).
    sentinel = tmp_path / "birdfy_mode_off"
    monkeypatch.setattr(main, "OFF_SENTINEL", str(sentinel))

    main._set_off_sentinel(False)  # absent -> still absent, no raise
    assert not sentinel.exists()

    main._set_off_sentinel(True)
    assert sentinel.exists()
    main._set_off_sentinel(True)  # idempotent
    assert sentinel.exists()

    main._set_off_sentinel(False)
    assert not sentinel.exists()


@pytest.mark.asyncio
async def test_main_short_session_walks_backoff_ladder(monkeypatch):
    # A clean-but-short session (< SESSION_OK_SECONDS) advances the failure
    # ladder: the first short session sleeps BACKOFF_SCHEDULE[0].
    monkeypatch.setattr(main, "MqttControl", lambda cfg: _ModeMqtt("auto"))
    monkeypatch.setattr(main, "MqttConfig", lambda: object())

    async def fake_run_once(mqtt):
        return  # returns immediately -> elapsed ~0 -> short session

    monkeypatch.setattr(main, "run_once", fake_run_once)

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main.main()
    assert sleeps == [main.BACKOFF_SCHEDULE[0]]


# --- session state poller + non-Addx branch -------------------------------

@pytest.mark.asyncio
async def test_publish_state_from_ticket(monkeypatch):
    async def fake_select(ticket):
        return {"online": 1, "awake": 1, "batteryLevel": 73}

    monkeypatch.setattr(main, "select_single_device", fake_select)
    spy = _SpyMqtt()
    await main._publish_state_from_ticket(spy, {"_addx_sn": "SN1"})
    assert spy.states and spy.states[0]["battery_level"] == 73


@pytest.mark.asyncio
async def test_session_state_poller_publishes_then_cancels(monkeypatch):
    monkeypatch.setattr(main, "SESSION_STATE_POLL_SECONDS", 1)

    published = []

    async def fake_publish(mqtt, ticket):
        published.append(ticket)

    monkeypatch.setattr(main, "_publish_state_from_ticket", fake_publish)

    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)

    task = asyncio.ensure_future(main._session_state_poller(_SpyMqtt(), {"t": 1}))
    for _ in range(50):
        await real_sleep(0)
        if published:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert published, "poller never refreshed state"


@pytest.mark.asyncio
async def test_session_poller_swallows_publish_errors(monkeypatch):
    monkeypatch.setattr(main, "SESSION_STATE_POLL_SECONDS", 1)

    calls = {"n": 0}

    async def boom(mqtt, ticket):
        calls["n"] += 1
        raise RuntimeError("cloud hiccup")

    monkeypatch.setattr(main, "_publish_state_from_ticket", boom)

    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)

    task = asyncio.ensure_future(main._session_state_poller(_SpyMqtt(), {}))
    for _ in range(50):
        await real_sleep(0)
        if calls["n"] >= 1:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The error was swallowed — the loop kept running rather than propagating.
    assert calls["n"] >= 1


@pytest.mark.asyncio
async def test_run_once_non_addx_device_raises_not_implemented(monkeypatch):
    non_addx = {
        "serialNumber": "SN9",
        "deviceName": "KVS Cam",
        "onAddx": False,
        "region": "us-east-1",
    }

    async def fake_login(email, password):
        return ({"userID": 1, "region": "us-east-1"}, [non_addx])

    monkeypatch.setattr(main, "login_or_resume", fake_login)
    with pytest.raises(RuntimeError, match="KVS WebRTC"):
        await main.run_once(_SpyMqtt())


@pytest.mark.asyncio
async def test_run_once_no_devices_raises(monkeypatch):
    async def fake_login(email, password):
        return ({"userID": 1}, [])

    monkeypatch.setattr(main, "login_or_resume", fake_login)
    with pytest.raises(RuntimeError, match="No devices"):
        await main.run_once(_SpyMqtt())
