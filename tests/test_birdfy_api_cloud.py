"""Unit tests for birdfy_api's cloud calls and the auth-token cache.

These hit no network — aiohttp.ClientSession is replaced with a fake that pops
queued (status, body) responses (see tests/_fake_aiohttp.py). They cover the
success and error branches of every authenticated call plus the resume/refresh
decision tree, which is the logic most likely to silently break the bridge's
ability to authenticate.
"""
import json

import pytest

import birdfy_api
from birdfy_api import (
    AuthExpiredError,
    RefreshFailedError,
    get_addx_ticket,
    get_devices,
    get_stream_play,
    login,
    login_or_resume,
    refresh_token,
    stop_live,
)

from ._fake_aiohttp import make_session_factory


@pytest.fixture
def patch_session(monkeypatch):
    """Return a function that installs a fake ClientSession with queued responses."""

    def _install(responses):
        factory = make_session_factory(responses)
        monkeypatch.setattr(birdfy_api.aiohttp, "ClientSession", factory)
        return factory.session

    return _install


@pytest.fixture(autouse=True)
def no_token_cache(monkeypatch, tmp_path):
    # Point the cache at a temp file and don't let a real one interfere.
    monkeypatch.setattr(birdfy_api, "_AUTH_CACHE_FILE", tmp_path / ".auth.json")
    monkeypatch.delenv("NVS_NO_TOKEN_CACHE", raising=False)
    monkeypatch.delenv("NVS_NO_TOKEN_REFRESH", raising=False)


def _wrap(data: dict) -> str:
    return json.dumps({"code": 0, "data": data})


# --- login ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_success(patch_session):
    patch_session([(200, _wrap({"token": "T", "userID": 7, "region": "us-east-1"}))])
    data = await login("e@x.com", "pw")
    assert data["token"] == "T"
    assert data["userID"] == 7


@pytest.mark.asyncio
async def test_login_non_200_raises(patch_session):
    patch_session([(401, '{"msg":"bad creds"}')])
    with pytest.raises(RuntimeError, match="Auth HTTP 401"):
        await login("e@x.com", "pw")


@pytest.mark.asyncio
async def test_login_missing_token_raises(patch_session):
    patch_session([(200, _wrap({"userID": 7}))])  # no token
    with pytest.raises(RuntimeError, match="no 'token' field"):
        await login("e@x.com", "pw")


@pytest.mark.asyncio
async def test_login_transport_error_wrapped(patch_session):
    patch_session([ConnectionError("boom")])
    with pytest.raises(RuntimeError, match="Auth request failed"):
        await login("e@x.com", "pw")


# --- get_devices ----------------------------------------------------------

_AUTH = {"token": "T", "userID": 7, "localEndpoint": "https://lw.example"}


@pytest.mark.asyncio
async def test_get_devices_success(patch_session):
    patch_session([(200, _wrap({"devices": [{"serialNumber": "SN1"}]}))])
    devices = await get_devices(_AUTH)
    assert devices == [{"serialNumber": "SN1"}]


@pytest.mark.asyncio
async def test_get_devices_401_raises_auth_expired(patch_session):
    patch_session([(401, '{"msg":"expired"}')])
    with pytest.raises(AuthExpiredError):
        await get_devices(_AUTH)


@pytest.mark.asyncio
async def test_get_devices_wrapped_auth_code_raises_auth_expired(patch_session):
    # 200 body but a non-zero auth code and no devices -> auth-expired.
    patch_session([(200, json.dumps({"code": 1002, "msg": "token expired"}))])
    with pytest.raises(AuthExpiredError):
        await get_devices(_AUTH)


@pytest.mark.asyncio
async def test_get_devices_other_non_200_raises_runtime(patch_session):
    patch_session([(500, "server error")])
    with pytest.raises(RuntimeError, match="Devices HTTP 500"):
        await get_devices(_AUTH)


@pytest.mark.asyncio
async def test_get_devices_builds_endpoint_from_region(patch_session):
    sess = patch_session([(200, _wrap({"devices": []}))])
    await get_devices({"token": "T", "userID": 7, "region": "eu-west-1"})
    (method, url, _kw) = sess.calls[0]
    assert url == "https://eu-west-1-localweb.nvts.co/v1/devices/v3"


# --- refresh_token --------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_disabled_raises(monkeypatch, patch_session):
    monkeypatch.setenv("NVS_NO_TOKEN_REFRESH", "1")
    with pytest.raises(RefreshFailedError, match="disabled"):
        await refresh_token({"refreshToken": "R"})


@pytest.mark.asyncio
async def test_refresh_no_refresh_token_raises(patch_session):
    with pytest.raises(RefreshFailedError, match="no refreshToken"):
        await refresh_token({"token": "T"})


@pytest.mark.asyncio
async def test_refresh_success_first_shape(patch_session):
    cached = {"refreshToken": "R", "token": "old", "userID": 7, "region": "us-east-1",
              "localEndpoint": "https://lw.example"}
    patch_session([(200, _wrap({"token": "NEW"}))])
    merged = await refresh_token(cached)
    assert merged["token"] == "NEW"
    assert merged["refreshToken"] == "R"  # retained when not rotated
    assert merged["region"] == "us-east-1"  # merged from cached


@pytest.mark.asyncio
async def test_refresh_rejected_code_raises(patch_session):
    cached = {"refreshToken": "R", "token": "old", "localEndpoint": "https://lw.example"}
    # First shape returns a hard rejection code -> stop trying.
    patch_session([(200, json.dumps({"code": 403}))])
    with pytest.raises(RefreshFailedError, match="rejected"):
        await refresh_token(cached)


@pytest.mark.asyncio
async def test_refresh_all_shapes_fail_raises(patch_session):
    cached = {"refreshToken": "R", "token": "old", "localEndpoint": "https://lw.example"}
    # Four 404s (one per candidate shape) -> exhausted.
    patch_session([(404, "nope")] * 4)
    with pytest.raises(RefreshFailedError, match="no refresh endpoint"):
        await refresh_token(cached)


# --- login_or_resume ------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_uses_valid_cache(patch_session):
    birdfy_api._save_cached_auth({"token": "C", "userID": 7}, "e@x.com")
    patch_session([(200, _wrap({"devices": [{"serialNumber": "SN1"}]}))])
    auth, devices = await login_or_resume("e@x.com", "pw")
    assert auth["token"] == "C"
    assert devices == [{"serialNumber": "SN1"}]


@pytest.mark.asyncio
async def test_resume_falls_back_to_login_when_no_cache(patch_session):
    sess = patch_session([
        (200, _wrap({"token": "FRESH", "userID": 7})),         # login
        (200, _wrap({"devices": [{"serialNumber": "SN1"}]})),  # get_devices
    ])
    auth, devices = await login_or_resume("e@x.com", "pw")
    assert auth["token"] == "FRESH"
    assert [c[0] for c in sess.calls] == ["POST", "GET"]  # login POST, devices GET


@pytest.mark.asyncio
async def test_resume_expired_cache_refreshes(patch_session):
    birdfy_api._save_cached_auth(
        {"token": "OLD", "refreshToken": "R", "userID": 7,
         "localEndpoint": "https://lw.example"},
        "e@x.com",
    )
    sess = patch_session([
        (401, "expired"),                                      # get_devices(cached) -> AuthExpired
        (200, _wrap({"token": "NEW"})),                        # refresh_token first shape
        (200, _wrap({"devices": [{"serialNumber": "SN1"}]})),  # get_devices(refreshed)
    ])
    auth, devices = await login_or_resume("e@x.com", "pw")
    assert auth["token"] == "NEW"
    assert devices and devices[0]["serialNumber"] == "SN1"
    assert len(sess.calls) == 3


@pytest.mark.asyncio
async def test_resume_different_account_clears_and_logins(patch_session):
    birdfy_api._save_cached_auth({"token": "C", "userID": 7}, "other@x.com")
    sess = patch_session([
        (200, _wrap({"token": "FRESH", "userID": 9})),          # fresh login
        (200, _wrap({"devices": []})),                          # get_devices
    ])
    auth, _devices = await login_or_resume("e@x.com", "pw")
    assert auth["token"] == "FRESH"
    assert sess.calls[0][0] == "POST"  # went straight to login, no cache validate


# --- get_addx_ticket ------------------------------------------------------

_TICKET_DEVICE = {"addxSn": "SN1", "region": "us-east-1"}


@pytest.mark.asyncio
async def test_addx_ticket_success(patch_session):
    patch_session([
        (200, _wrap({"token": "ADDX", "endpoint": "https://addx.example/", "language": "en"})),
        (200, _wrap({"signalServer": "wss://s", "groupId": "g", "role": "viewer",
                     "id": "cid", "result": 0})),
    ])
    ticket = await get_addx_ticket(_AUTH, device=_TICKET_DEVICE)
    assert ticket["signalServer"] == "wss://s"
    assert ticket["_addx_token"] == "ADDX"
    assert ticket["_addx_endpoint"] == "https://addx.example/"
    assert ticket["_addx_sn"] == "SN1"
    # addx_state out-param is populated when provided.
    state = {}
    patch_session([
        (200, _wrap({"token": "ADDX", "endpoint": "https://addx.example/"})),
        (200, _wrap({"signalServer": "wss://s", "groupId": "g", "role": "v", "id": "c", "result": 0})),
    ])
    await get_addx_ticket(_AUTH, device=_TICKET_DEVICE, addx_state=state)
    assert state["_addx_token"] == "ADDX"


@pytest.mark.asyncio
async def test_addx_ticket_all_auth_styles_fail(patch_session):
    # Four 403s (one per header-candidate) -> RuntimeError.
    patch_session([(403, "denied")] * 4)
    with pytest.raises(RuntimeError, match="all auth styles"):
        await get_addx_ticket(_AUTH, device=_TICKET_DEVICE)


@pytest.mark.asyncio
async def test_addx_ticket_missing_token_raises(patch_session):
    # An empty endpoint becomes "/" (truthy) after rstrip+"/", so the guard fires
    # on the missing token. A 200 with neither token nor endpoint hits it.
    patch_session([(200, _wrap({"language": "en"}))])
    with pytest.raises(RuntimeError, match="missing 'token' or 'endpoint'"):
        await get_addx_ticket(_AUTH, device=_TICKET_DEVICE)


@pytest.mark.asyncio
async def test_addx_ticket_nonzero_result_raises(patch_session):
    patch_session([
        (200, _wrap({"token": "ADDX", "endpoint": "https://addx.example/"})),
        (200, _wrap({"result": 1234})),  # ticket error
    ])
    with pytest.raises(RuntimeError, match="ticket error result=1234"):
        await get_addx_ticket(_AUTH, device=_TICKET_DEVICE)


# --- stop_live ------------------------------------------------------------

_STOP_TICKET = {
    "_addx_endpoint": "https://addx.example/",
    "_addx_token": "ADDX",
    "_addx_sn": "SN1",
    "_language": "en",
    "_country_no": "us",
}


@pytest.mark.asyncio
async def test_stop_live_success(patch_session):
    patch_session([(200, _wrap({}))])
    assert await stop_live(_STOP_TICKET) is True


@pytest.mark.asyncio
async def test_stop_live_non_200_returns_false(patch_session):
    patch_session([(500, "err")])
    assert await stop_live(_STOP_TICKET) is False


@pytest.mark.asyncio
async def test_stop_live_exception_returns_false(patch_session):
    patch_session([ConnectionError("down")])
    assert await stop_live(_STOP_TICKET) is False


# --- get_stream_play ------------------------------------------------------

@pytest.mark.asyncio
async def test_get_stream_play_success(patch_session):
    patch_session([(200, _wrap({"channel": "ch", "region": "us-east-1"}))])
    data = await get_stream_play(_AUTH, {"serialNumber": "SN1"})
    assert data["channel"] == "ch"


@pytest.mark.asyncio
async def test_get_stream_play_non_200_raises(patch_session):
    patch_session([(403, "denied")])
    with pytest.raises(RuntimeError, match="Stream play HTTP 403"):
        await get_stream_play(_AUTH, {"serialNumber": "SN1"})
