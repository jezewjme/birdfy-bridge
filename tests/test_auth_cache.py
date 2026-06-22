"""Tests for the auth-token cache helpers in birdfy_api.

The cache lets the bridge resume without a fresh /users/login/v2 (which sends a
"new device logged in" email). A regression that silently failed to read/write
it would email the user on every restart; one that returned a stale/foreign
cache would attempt auth with a wrong token.
"""
import json

import pytest

import birdfy_api


@pytest.fixture
def cache_file(monkeypatch, tmp_path):
    f = tmp_path / ".birdfy_auth_cache.json"
    monkeypatch.setattr(birdfy_api, "_AUTH_CACHE_FILE", f)
    monkeypatch.delenv("NVS_NO_TOKEN_CACHE", raising=False)
    return f


def test_save_then_load_roundtrip(cache_file):
    birdfy_api._save_cached_auth({"token": "T", "userID": 7}, "e@x.com")
    loaded = birdfy_api._load_cached_auth()
    assert loaded["token"] == "T"
    assert loaded["_cached_email"] == "e@x.com"
    assert "_cached_at" in loaded


def test_load_missing_file_returns_none(cache_file):
    assert not cache_file.exists()
    assert birdfy_api._load_cached_auth() is None


def test_load_corrupt_file_returns_none(cache_file):
    cache_file.write_text("{not json", encoding="utf-8")
    assert birdfy_api._load_cached_auth() is None


def test_load_without_token_returns_none(cache_file):
    cache_file.write_text(json.dumps({"userID": 7}), encoding="utf-8")
    assert birdfy_api._load_cached_auth() is None


def test_no_cache_env_disables_load_and_save(cache_file, monkeypatch):
    monkeypatch.setenv("NVS_NO_TOKEN_CACHE", "1")
    birdfy_api._save_cached_auth({"token": "T"}, "e@x.com")
    assert not cache_file.exists()  # save was a no-op
    assert birdfy_api._load_cached_auth() is None  # load short-circuits


def test_save_preserves_existing_email_when_empty(cache_file):
    # The refresh path passes email="" and relies on data carrying _cached_email.
    birdfy_api._save_cached_auth({"token": "T", "_cached_email": "keep@x.com"}, "")
    loaded = birdfy_api._load_cached_auth()
    assert loaded["_cached_email"] == "keep@x.com"


def test_clear_removes_file(cache_file):
    birdfy_api._save_cached_auth({"token": "T"}, "e@x.com")
    assert cache_file.exists()
    birdfy_api._clear_cached_auth()
    assert not cache_file.exists()


def test_clear_missing_file_is_safe(cache_file):
    # No file present — must not raise.
    birdfy_api._clear_cached_auth()


def test_nvs_headers_shape(cache_file):
    h = birdfy_api._nvs_headers(token="T", user_id="7")
    assert h["x-nvs-userid"] == "7"
    assert len(h["x-nvs-signature"]) == 64
    assert h["x-nvs-version"] == '{"signature":2}'
    assert "x-nvs-time" in h and h["x-nvs-time"].isdigit()
