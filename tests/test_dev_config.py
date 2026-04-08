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


@pytest.mark.parametrize(
    "preset_name",
    list(PRESETS.keys()),
    ids=list(PRESETS.keys()),
)
def test_preset_loads_and_seeds(tmp_path, preset_name):
    """Each preset can be loaded, seeded, and produces the expected canvas."""
    dev_dir = tmp_path / "dev"
    with patch("claude_rts.dev_config.DEV_CONFIG_DIR", dev_dir):
        setup_dev_config(preset=preset_name)

    config = json.loads((dev_dir / "config.json").read_text())
    assert "startup_script" in config
    assert "default_canvas" in config

    preset = PRESETS[preset_name]
    for canvas_name in preset["canvases"]:
        canvas_path = dev_dir / "canvases" / f"{canvas_name}.json"
        assert canvas_path.exists(), f"Canvas '{canvas_name}' not written for preset '{preset_name}'"
        canvas = json.loads(canvas_path.read_text())
        assert "cards" in canvas


def test_stress_test_preset_card_variety(tmp_path):
    """stress-test preset has >= 5 cards with both terminal and widget types."""
    dev_dir = tmp_path / "dev"
    with patch("claude_rts.dev_config.DEV_CONFIG_DIR", dev_dir):
        setup_dev_config(preset="stress-test")

    canvas = json.loads((dev_dir / "canvases" / "stress-layout.json").read_text())
    cards = canvas["cards"]
    assert len(cards) >= 5, f"Expected >= 5 cards, got {len(cards)}"

    types = {c["type"] for c in cards}
    assert "terminal" in types, "stress-test must include terminal cards"
    assert "widget" in types, "stress-test must include widget cards"

    # Verify widget subtypes
    widget_types = {c.get("widgetType") for c in cards if c["type"] == "widget"}
    assert "system-info" in widget_types
    assert "profiles" in widget_types

    # Verify at least one card is far from origin (forces panning)
    far_cards = [c for c in cards if c["x"] >= 2000 or c["y"] >= 2000]
    assert len(far_cards) >= 1, "At least one card should be far from origin"
