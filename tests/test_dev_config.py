"""Tests for dev_config fixture setup."""

import json
from unittest.mock import patch

import pytest

from claude_rts.dev_config import setup_dev_config, _FIXTURE_CONFIG, _FIXTURE_CANVASES, PRESETS


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


def test_setup_dev_config_profiles_preset(tmp_path):
    dev_dir = tmp_path / "dev"
    with patch("claude_rts.dev_config.DEV_CONFIG_DIR", dev_dir):
        setup_dev_config(preset="profiles")

    config = json.loads((dev_dir / "config.json").read_text())
    assert config["default_canvas"] == "profiles-dev"
    canvas = json.loads((dev_dir / "canvases" / "profiles-dev.json").read_text())
    assert any(c.get("widgetType") == "profiles" for c in canvas["cards"])


def test_setup_dev_config_unknown_preset_raises(tmp_path):
    dev_dir = tmp_path / "dev"
    with patch("claude_rts.dev_config.DEV_CONFIG_DIR", dev_dir):
        with pytest.raises(ValueError, match="Unknown dev-config preset"):
            setup_dev_config(preset="nonexistent")


def test_all_presets_have_required_keys():
    for name, preset in PRESETS.items():
        assert "config" in preset, f"Preset '{name}' missing 'config' key"
        assert "canvases" in preset, f"Preset '{name}' missing 'canvases' key"
        assert "startup_script" in preset["config"], f"Preset '{name}' config missing 'startup_script'"
