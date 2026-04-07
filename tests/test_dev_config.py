"""Tests for dev_config fixture setup."""

import json
from unittest.mock import patch

from claude_rts.dev_config import setup_dev_config, _FIXTURE_CONFIG, _FIXTURE_CANVASES


def test_setup_dev_config_creates_dir(tmp_path):
    dev_dir = tmp_path / "dev"
    with patch("claude_rts.dev_config.DEV_CONFIG_DIR", dev_dir):
        result = setup_dev_config()

    assert result == dev_dir
    assert dev_dir.is_dir()
    assert (dev_dir / "canvases").is_dir()


def test_setup_dev_config_writes_config(tmp_path):
    dev_dir = tmp_path / "dev"
    with patch("claude_rts.dev_config.DEV_CONFIG_DIR", dev_dir):
        setup_dev_config()

    config = json.loads((dev_dir / "config.json").read_text())
    assert config["startup_script"] == _FIXTURE_CONFIG["startup_script"]
    assert config["default_canvas"] == _FIXTURE_CONFIG["default_canvas"]
    assert config["probe_profiles"] == []


def test_setup_dev_config_writes_canvases(tmp_path):
    dev_dir = tmp_path / "dev"
    with patch("claude_rts.dev_config.DEV_CONFIG_DIR", dev_dir):
        setup_dev_config()

    for name, layout in _FIXTURE_CANVASES.items():
        path = dev_dir / "canvases" / f"{name}.json"
        assert path.exists()
        assert json.loads(path.read_text()) == layout


def test_setup_dev_config_wipes_existing(tmp_path):
    dev_dir = tmp_path / "dev"
    dev_dir.mkdir()
    stale = dev_dir / "stale.json"
    stale.write_text("{}")

    with patch("claude_rts.dev_config.DEV_CONFIG_DIR", dev_dir):
        setup_dev_config()

    assert not stale.exists()


def test_setup_dev_config_idempotent(tmp_path):
    dev_dir = tmp_path / "dev"
    with patch("claude_rts.dev_config.DEV_CONFIG_DIR", dev_dir):
        setup_dev_config()
        setup_dev_config()  # second call should not error

    assert (dev_dir / "config.json").exists()
