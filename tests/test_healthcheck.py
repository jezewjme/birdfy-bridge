"""Tests for the container healthcheck (docker/healthcheck.py).

The healthcheck decides container health from MediaMTX's publish state, with a
grace window so normal reconnects/camera-sleep don't flap it. The key behavior
added here: an `off`-mode sentinel must short-circuit to HEALTHY, because in off
mode the bridge intentionally never publishes — without this the grace window
would expire and Docker would restart a perfectly healthy container.

healthcheck.py lives under docker/ and isn't on the import path, so we load it by
file path. It's stdlib-only, so there's nothing to install.
"""
import importlib.util
import time
from pathlib import Path

import pytest

_HC_PATH = Path(__file__).resolve().parent.parent / "docker" / "healthcheck.py"


@pytest.fixture
def hc(monkeypatch, tmp_path):
    """Load healthcheck.py fresh with its file-backed state pointed at tmp."""
    spec = importlib.util.spec_from_file_location("birdfy_healthcheck", _HC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Redirect both the down-since state file and the off sentinel into tmp so the
    # test never reads or writes the real /tmp defaults.
    monkeypatch.setattr(mod, "_STATE_FILE", str(tmp_path / "down_since"))
    monkeypatch.setattr(mod, "_OFF_SENTINEL", str(tmp_path / "mode_off"))
    return mod


def test_off_sentinel_forces_healthy_even_when_api_down(hc, monkeypatch):
    # The off sentinel must short-circuit BEFORE the API-alive check: in off mode a
    # non-publishing (or even idle) MediaMTX is expected, so we stay healthy.
    Path(hc._OFF_SENTINEL).write_text("off\n")
    monkeypatch.setattr(hc, "_api_alive", lambda: False)
    monkeypatch.setattr(hc, "_path_ready", lambda: False)
    assert hc.main() == 0


def test_off_sentinel_clears_down_timer(hc, monkeypatch):
    # Entering off while a down-timer was running must reset it, so leaving off
    # later starts the grace window fresh rather than instantly tripping it.
    hc._write_down_since(time.time() - 9999)
    Path(hc._OFF_SENTINEL).write_text("off\n")
    assert hc.main() == 0
    assert hc._read_down_since() is None


def test_api_down_is_immediately_unhealthy(hc, monkeypatch):
    monkeypatch.setattr(hc, "_api_alive", lambda: False)
    assert hc.main() == 1


def test_publishing_is_healthy_and_resets_timer(hc, monkeypatch):
    monkeypatch.setattr(hc, "_api_alive", lambda: True)
    monkeypatch.setattr(hc, "_path_ready", lambda: True)
    hc._write_down_since(time.time() - 100)
    assert hc.main() == 0
    assert hc._read_down_since() is None  # cleared on a healthy publish


def test_not_publishing_within_grace_is_healthy(hc, monkeypatch):
    monkeypatch.setattr(hc, "_api_alive", lambda: True)
    monkeypatch.setattr(hc, "_path_ready", lambda: False)
    monkeypatch.setattr(hc, "GRACE_SEC", 300)
    # First call records down-since now; well within grace -> healthy.
    assert hc.main() == 0
    assert hc._read_down_since() is not None


def test_not_publishing_past_grace_is_unhealthy(hc, monkeypatch):
    monkeypatch.setattr(hc, "_api_alive", lambda: True)
    monkeypatch.setattr(hc, "_path_ready", lambda: False)
    monkeypatch.setattr(hc, "GRACE_SEC", 300)
    hc._write_down_since(time.time() - 301)  # already down longer than grace
    assert hc.main() == 1
