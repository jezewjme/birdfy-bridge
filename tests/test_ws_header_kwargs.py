"""Tests for the websockets header-kwarg compatibility shim.

websockets renamed extra_headers -> additional_headers in 14.0 and removed the
old name; passing the wrong one raises TypeError and silently killed the
signaling WS once the image resolved a newer websockets despite the pin. The
shim picks the right name from connect()'s signature. These tests lock both
branches so a future refactor can't reintroduce the hardcoded name.
"""
import inspect

import webrtc_client
from webrtc_client import _ws_header_kwargs

_HEADERS = {"User-Agent": "x"}


def test_picks_name_accepted_by_installed_connect():
    # Whatever name the helper returns must be a real parameter of the actually
    # installed websockets.connect — otherwise connect() raises TypeError.
    kw = _ws_header_kwargs(_HEADERS)
    (name,) = kw.keys()
    assert name in ("extra_headers", "additional_headers")
    params = inspect.signature(webrtc_client.websockets.connect).parameters
    # `additional_headers` (>=14) is an explicit param; on legacy <14
    # `extra_headers` is too. Either way the chosen name must be bindable.
    assert name in params or any(
        p.kind == p.VAR_KEYWORD for p in params.values()
    )
    assert kw[name] is _HEADERS


def test_prefers_additional_headers_when_available(monkeypatch):
    def fake_connect(uri, *, additional_headers=None, ping_interval=None):  # noqa: ARG001
        ...

    monkeypatch.setattr(webrtc_client.websockets, "connect", fake_connect)
    assert _ws_header_kwargs(_HEADERS) == {"additional_headers": _HEADERS}


def test_falls_back_to_extra_headers_on_legacy(monkeypatch):
    def fake_connect(uri, *, extra_headers=None, ping_interval=None):  # noqa: ARG001
        ...

    monkeypatch.setattr(webrtc_client.websockets, "connect", fake_connect)
    assert _ws_header_kwargs(_HEADERS) == {"extra_headers": _HEADERS}
