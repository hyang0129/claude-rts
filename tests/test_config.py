"""Tests for config module and API endpoints."""

import json
from unittest.mock import patch

import pytest

from claude_rts.config import (
    DEFAULT_CONFIG,
    read_config,
    write_config,
    list_canvases,
    read_canvas,
    write_canvas,
    delete_canvas,
    _valid_canvas_name,
)
from claude_rts.server import create_app


# ── Unit tests for config module ──────────────────────────


@pytest.fixture
def config_dir(tmp_path):
    """Patch CONFIG_DIR, CONFIG_FILE, CANVASES_DIR to use tmp_path."""
    cfg_dir = tmp_path / ".claude-rts"
    cfg_file = cfg_dir / "config.json"
    canvases_dir = cfg_dir / "canvases"
    with patch("claude_rts.config.CONFIG_DIR", cfg_dir), \
         patch("claude_rts.config.CONFIG_FILE", cfg_file), \
         patch("claude_rts.config.CANVASES_DIR", canvases_dir):
        yield cfg_dir, cfg_file, canvases_dir


def test_read_config_defaults(config_dir):
    """Returns defaults when no config file exists."""
    data = read_config()
    assert data == DEFAULT_CONFIG


def test_write_and_read_config(config_dir):
    cfg_dir, cfg_file, _ = config_dir
    write_config({"copy": "auto-select", "idle_threshold": 10})
    data = read_config()
    assert data["copy"] == "auto-select"
    assert data["idle_threshold"] == 10
    # Defaults should fill in missing keys
    assert data["paste"] == DEFAULT_CONFIG["paste"]
    assert data["theme"] == DEFAULT_CONFIG["theme"]


def test_write_config_merges_defaults(config_dir):
    """write_config merges with defaults so file always has all keys."""
    result = write_config({"copy": "ctrl-c-sel"})
    assert result["copy"] == "ctrl-c-sel"
    assert result["paste"] == DEFAULT_CONFIG["paste"]


def test_read_config_corrupt_json(config_dir):
    """Corrupt JSON falls back to defaults."""
    cfg_dir, cfg_file, _ = config_dir
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text("not json!", encoding="utf-8")
    data = read_config()
    assert data == DEFAULT_CONFIG


def test_valid_canvas_name():
    assert _valid_canvas_name("main") is True
    assert _valid_canvas_name("my-layout_2") is True
    assert _valid_canvas_name("") is False
    assert _valid_canvas_name("../etc") is False
    assert _valid_canvas_name("foo bar") is False
    assert _valid_canvas_name("a/b") is False


def test_list_canvases_empty(config_dir):
    names = list_canvases()
    assert names == []


def test_write_and_list_canvases(config_dir):
    layout = {"name": "main", "canvas_size": [3840, 2160], "cards": []}
    assert write_canvas("main", layout) is True
    assert write_canvas("work", {"name": "work", "cards": []}) is True
    names = list_canvases()
    assert names == ["main", "work"]


def test_read_canvas(config_dir):
    layout = {"name": "test", "canvas_size": [3840, 2160], "cards": [{"hub": "hub_1"}]}
    write_canvas("test", layout)
    data = read_canvas("test")
    assert data == layout


def test_read_canvas_not_found(config_dir):
    assert read_canvas("nonexistent") is None


def test_read_canvas_invalid_name(config_dir):
    assert read_canvas("../evil") is None


def test_write_canvas_invalid_name(config_dir):
    assert write_canvas("bad name!", {"cards": []}) is False


def test_delete_canvas(config_dir):
    write_canvas("deleteme", {"cards": []})
    assert delete_canvas("deleteme") is True
    assert read_canvas("deleteme") is None


def test_delete_canvas_not_found(config_dir):
    assert delete_canvas("nope") is False


# ── API endpoint tests ────────────────────────────────────


@pytest.fixture
def app(config_dir):
    return create_app()


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


async def test_get_config_returns_defaults(client, config_dir):
    resp = await client.get("/api/config")
    assert resp.status == 200
    data = await resp.json()
    assert data["copy"] == DEFAULT_CONFIG["copy"]
    assert data["idle_threshold"] == DEFAULT_CONFIG["idle_threshold"]


async def test_put_config(client, config_dir):
    resp = await client.put(
        "/api/config",
        json={"copy": "auto-select", "idle_threshold": 30},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["copy"] == "auto-select"
    assert data["idle_threshold"] == 30
    assert data["paste"] == DEFAULT_CONFIG["paste"]

    # Verify it persisted
    resp2 = await client.get("/api/config")
    data2 = await resp2.json()
    assert data2["copy"] == "auto-select"


async def test_put_config_invalid_json(client, config_dir):
    resp = await client.put(
        "/api/config",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_list_canvases_empty(client, config_dir):
    resp = await client.get("/api/canvases")
    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_put_and_get_canvas(client, config_dir):
    layout = {
        "name": "main",
        "canvas_size": [3840, 2160],
        "cards": [{"type": "terminal", "hub": "hub_1", "x": 100, "y": 100, "w": 720, "h": 480}],
    }
    resp = await client.put("/api/canvases/main", json=layout)
    assert resp.status == 200
    result = await resp.json()
    assert result["status"] == "ok"
    assert result["name"] == "main"

    # Read it back
    resp2 = await client.get("/api/canvases/main")
    assert resp2.status == 200
    data = await resp2.json()
    assert data["name"] == "main"
    assert len(data["cards"]) == 1

    # Should appear in list
    resp3 = await client.get("/api/canvases")
    names = await resp3.json()
    assert "main" in names


async def test_get_canvas_not_found(client, config_dir):
    resp = await client.get("/api/canvases/nonexistent")
    assert resp.status == 404


async def test_put_canvas_invalid_json(client, config_dir):
    resp = await client.put(
        "/api/canvases/main",
        data=b"bad",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_delete_canvas_via_api(client, config_dir):
    """DELETE /api/canvases/{name} removes a canvas."""
    # Create a canvas first
    layout = {"name": "temp", "canvas_size": [3840, 2160], "cards": []}
    resp = await client.put("/api/canvases/temp", json=layout)
    assert resp.status == 200

    # Verify it exists
    resp = await client.get("/api/canvases/temp")
    assert resp.status == 200

    # Delete it
    resp = await client.delete("/api/canvases/temp")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["name"] == "temp"

    # Verify it's gone
    resp = await client.get("/api/canvases/temp")
    assert resp.status == 404


async def test_delete_canvas_not_found_via_api(client, config_dir):
    """DELETE returns 404 for non-existent canvas."""
    resp = await client.delete("/api/canvases/nonexistent")
    assert resp.status == 404


async def test_delete_main_canvas_forbidden(client, config_dir):
    """DELETE returns 400 when trying to delete 'main' canvas."""
    # Create main canvas first
    layout = {"name": "main", "canvas_size": [3840, 2160], "cards": []}
    await client.put("/api/canvases/main", json=layout)

    resp = await client.delete("/api/canvases/main")
    assert resp.status == 400


async def test_app_has_config_and_canvas_routes(app):
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, 'resource')]
    assert "/api/config" in routes
    assert "/api/canvases" in routes
    assert "/api/canvases/{name}" in routes
