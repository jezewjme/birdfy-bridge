"""Tests for the pure message/URL/config builders in webrtc_client.

These shape the signaling traffic to match what the browser sends; the camera
rejects anything off-shape, so locking the exact dict/URL layout catches a
regression before it costs a live debugging session.
"""
import base64
import json

import pytest

pytest.importorskip("aiortc", reason="webrtc_client imports aiortc at module load")

from webrtc_client import (  # noqa: E402
    _b64_encode,
    _build_ws_url,
    _ice_candidate_msg,
    _make_ice_config,
    _sdp_offer_msg,
)

_TICKET = {
    "signalServer": "wss://sig.example",
    "groupId": "G",
    "role": "viewer",
    "id": "CID",
    "traceId": "TR",
    "time": "123",
    "sign": "SIG",
}


def _decode_payload(msg):
    return json.loads(base64.b64decode(msg["messagePayload"]).decode())


# --- _build_ws_url --------------------------------------------------------

def test_ws_url_core_path_and_params():
    url = _build_ws_url(_TICKET)
    assert url.startswith("wss://sig.example/G/viewer/CID?")
    assert "traceId=TR" in url
    assert "time=123" in url
    assert "sign=SIG" in url
    assert url.endswith("&name=a4x")
    assert "accessToken" not in url  # none in this ticket


def test_ws_url_includes_access_token_when_present():
    url = _build_ws_url({**_TICKET, "accessToken": "AT"})
    assert "accessToken=AT" in url


# --- _make_ice_config -----------------------------------------------------

def test_ice_config_always_has_google_stun_and_max_bundle():
    cfg = _make_ice_config({})
    urls = [u for s in cfg.iceServers for u in (s.urls if isinstance(s.urls, list) else [s.urls])]
    assert any("stun:stun.l.google.com" in u for u in urls)
    # MAX_BUNDLE is required for this camera's single-transport handshake.
    assert cfg.bundlePolicy.value == "max-bundle"


def test_ice_config_appends_ticket_servers():
    cfg = _make_ice_config({
        "iceServer": [
            {"url": "turn:t.example:3478", "username": "u", "credential": "c"},
        ]
    })
    found = [s for s in cfg.iceServers if "turn:t.example:3478" in (
        s.urls if isinstance(s.urls, list) else [s.urls]
    )]
    assert found and found[0].username == "u" and found[0].credential == "c"


def test_ice_config_accepts_urls_plural_and_string():
    # Both "iceServers"/"urls" and a bare string url must be handled.
    cfg = _make_ice_config({"iceServers": [{"urls": "turn:plural.example"}]})
    assert any("turn:plural.example" in (
        s.urls if isinstance(s.urls, list) else [s.urls]
    ) for s in cfg.iceServers)


def test_ice_config_skips_server_without_urls():
    cfg = _make_ice_config({"iceServer": [{"username": "u"}]})  # no url
    # Only the default google STUN remains.
    assert len(cfg.iceServers) == 1


# --- _b64_encode ----------------------------------------------------------

def test_b64_encode_roundtrips():
    obj = {"a": 1, "b": "x"}
    assert json.loads(base64.b64decode(_b64_encode(obj)).decode()) == obj


# --- _sdp_offer_msg -------------------------------------------------------

def test_sdp_offer_msg_shape():
    msg = _sdp_offer_msg("v=0...", "RECIP", "SEND", "SESS")
    assert msg["messageType"] == "SDP_OFFER"
    assert msg["recipientClientId"] == "RECIP"
    assert msg["senderClientId"] == "SEND"
    assert msg["sessionId"] == "SESS"
    assert msg["mode"] == "vicoo"
    payload = _decode_payload(msg)
    assert payload == {"sdp": "v=0...", "type": "offer"}


# --- _ice_candidate_msg ---------------------------------------------------

def test_ice_candidate_msg_shape():
    msg = _ice_candidate_msg(
        candidate_line="candidate:1 1 udp ...",
        sdp_mid="0",
        sdp_mline_index=0,
        ufrag="UF",
        recipient_id="RECIP",
        sender_id="SEND",
        session_id="SESS",
    )
    assert msg["messageType"] == "ICE_CANDIDATE"
    assert msg["recipientClientId"] == "RECIP"
    payload = _decode_payload(msg)
    assert payload["candidate"] == "candidate:1 1 udp ..."
    assert payload["sdpMid"] == "0"
    assert payload["sdpMLineIndex"] == 0
    assert payload["usernameFragment"] == "UF"
