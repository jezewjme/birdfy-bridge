"""Tests for select_single_device()'s HTTP-response handling in birdfy_api.py.

select_single_device wakes the camera's cloud subscription and, since this PR,
also carries the live device state the offline short-circuit and MQTT sensors
depend on. Its return contract is load-bearing:
  * HTTP 200 with a `data` object  -> that dict (truthy; state readable off it),
  * HTTP 200 with missing/non-dict data -> {} (still truthy = "succeeded"),
  * HTTP 200 with non-JSON body    -> {} (truthy, never raises),
  * non-200                        -> None (falsy = failure),
  * transport exception            -> None (best-effort, never raises).

A regression that returned None on a 200 would make every connect look like a
failure (back-off churn); one that returned {} on a non-200 would push the bridge
into a handshake against a cloud that just refused it.
"""
import aiohttp
import pytest

import birdfy_api

_TICKET = {
    "_addx_endpoint": "https://cloud.example/",
    "_addx_token": "tok",
    "_addx_sn": "SN1",
    "_language": "EN",
    "_country_no": "us",
    "_region": "us-east-1",
}


class _FakeResp:
    def __init__(self, status, text):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """Stands in for aiohttp.ClientSession; .post() returns a fake response CM."""

    def __init__(self, resp=None, raise_on_post=None):
        self._resp = resp
        self._raise = raise_on_post

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, *a, **k):
        if self._raise is not None:
            raise self._raise
        return self._resp


def _patch_session(monkeypatch, *, resp=None, raise_on_post=None):
    monkeypatch.setattr(
        birdfy_api.aiohttp,
        "ClientSession",
        lambda *a, **k: _FakeSession(resp=resp, raise_on_post=raise_on_post),
    )


@pytest.mark.asyncio
async def test_returns_data_dict_on_200(monkeypatch):
    body = '{"code": 0, "data": {"online": 1, "batteryLevel": 80}}'
    _patch_session(monkeypatch, resp=_FakeResp(200, body))
    data = await birdfy_api.select_single_device(_TICKET)
    assert data == {"online": 1, "batteryLevel": 80}


@pytest.mark.asyncio
async def test_returns_empty_dict_when_data_missing_on_200(monkeypatch):
    # 200 but no `data` key — still "succeeded", so a truthy empty dict.
    _patch_session(monkeypatch, resp=_FakeResp(200, '{"code": 0}'))
    data = await birdfy_api.select_single_device(_TICKET)
    assert data == {}


@pytest.mark.asyncio
async def test_returns_empty_dict_when_data_not_a_dict(monkeypatch):
    # `data` present but the wrong shape (a list) -> coerced to {} (truthy).
    _patch_session(monkeypatch, resp=_FakeResp(200, '{"data": [1, 2, 3]}'))
    data = await birdfy_api.select_single_device(_TICKET)
    assert data == {}


@pytest.mark.asyncio
async def test_returns_empty_dict_on_non_json_200(monkeypatch):
    # 200 with a non-JSON body must not raise — json.loads ValueError -> {}.
    _patch_session(monkeypatch, resp=_FakeResp(200, "not json at all"))
    data = await birdfy_api.select_single_device(_TICKET)
    assert data == {}


@pytest.mark.asyncio
async def test_returns_none_on_non_200(monkeypatch):
    _patch_session(monkeypatch, resp=_FakeResp(503, "service unavailable"))
    data = await birdfy_api.select_single_device(_TICKET)
    assert data is None  # falsy = failure, callers back off


@pytest.mark.asyncio
async def test_returns_none_on_transport_error(monkeypatch):
    # A connection error must be swallowed (best-effort) and read as failure.
    _patch_session(
        monkeypatch, raise_on_post=aiohttp.ClientConnectionError("refused")
    )
    data = await birdfy_api.select_single_device(_TICKET)
    assert data is None


@pytest.mark.asyncio
async def test_truthiness_contract_for_legacy_callers(monkeypatch):
    # The old API returned a bool; legacy call sites distinguish failure (None,
    # falsy) from a non-200. A populated 200 is truthy.
    _patch_session(monkeypatch, resp=_FakeResp(200, '{"data": {"online": 1}}'))
    assert bool(await birdfy_api.select_single_device(_TICKET)) is True
    # A non-200 returns None — unambiguously falsy = failure.
    _patch_session(monkeypatch, resp=_FakeResp(500, "err"))
    result = await birdfy_api.select_single_device(_TICKET)
    assert result is None
    assert bool(result) is False
