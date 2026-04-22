"""Tests for config module and API endpoints."""

import pytest

from claude_rts.config import (
    DEFAULT_CONFIG,
    load,
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
def app_config(tmp_path):
    """Return an AppConfig rooted in a temp directory."""
    return load(tmp_path / ".supreme-claudemander")


def test_read_config_defaults(app_config):
    """Returns defaults when no config file exists."""
    data = read_config(app_config)
    assert data == DEFAULT_CONFIG


def test_write_and_read_config(app_config):
    write_config(app_config, {"copy": "auto-select", "idle_threshold": 10})
    data = read_config(app_config)
    assert data["copy"] == "auto-select"
    assert data["idle_threshold"] == 10
    # Defaults should fill in missing keys
    assert data["paste"] == DEFAULT_CONFIG["paste"]
    assert data["theme"] == DEFAULT_CONFIG["theme"]


def test_write_config_merges_defaults(app_config):
    """write_config merges with defaults so file always has all keys."""
    result = write_config(app_config, {"copy": "ctrl-c-sel"})
    assert result["copy"] == "ctrl-c-sel"
    assert result["paste"] == DEFAULT_CONFIG["paste"]


def test_read_config_corrupt_json(app_config):
    """Corrupt JSON falls back to defaults."""
    app_config.config_dir.mkdir(parents=True, exist_ok=True)
    app_config.config_file.write_text("not json!", encoding="utf-8")
    data = read_config(app_config)
    assert data == DEFAULT_CONFIG


def test_valid_canvas_name():
    assert _valid_canvas_name("main") is True
    assert _valid_canvas_name("my-layout_2") is True
    assert _valid_canvas_name("") is False
    assert _valid_canvas_name("../etc") is False
    assert _valid_canvas_name("foo bar") is False
    assert _valid_canvas_name("a/b") is False


def test_list_canvases_empty(app_config):
    names = list_canvases(app_config)
    assert names == []


def test_write_and_list_canvases(app_config):
    layout = {"name": "main", "canvas_size": [3840, 2160], "cards": []}
    assert write_canvas(app_config, "main", layout) is True
    assert write_canvas(app_config, "work", {"name": "work", "cards": []}) is True
    names = list_canvases(app_config)
    assert names == ["main", "work"]


def test_read_canvas(app_config):
    layout = {"name": "test", "canvas_size": [3840, 2160], "cards": [{"hub": "hub_1"}]}
    write_canvas(app_config, "test", layout)
    data = read_canvas(app_config, "test")
    assert data == layout


def test_read_canvas_not_found(app_config):
    assert read_canvas(app_config, "nonexistent") is None


def test_read_canvas_invalid_name(app_config):
    assert read_canvas(app_config, "../evil") is None


def test_write_canvas_invalid_name(app_config):
    assert write_canvas(app_config, "bad name!", {"cards": []}) is False


def test_delete_canvas(app_config):
    write_canvas(app_config, "deleteme", {"cards": []})
    assert delete_canvas(app_config, "deleteme") is True
    assert read_canvas(app_config, "deleteme") is None


def test_delete_canvas_not_found(app_config):
    assert delete_canvas(app_config, "nope") is False


# ── API endpoint tests ────────────────────────────────────


@pytest.fixture
def app(app_config):
    return create_app(app_config)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


async def test_get_config_returns_defaults(client, app_config):
    resp = await client.get("/api/config")
    assert resp.status == 200
    data = await resp.json()
    assert data["copy"] == DEFAULT_CONFIG["copy"]
    assert data["idle_threshold"] == DEFAULT_CONFIG["idle_threshold"]


async def test_put_config(client, app_config):
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


async def test_put_config_invalid_json(client, app_config):
    resp = await client.put(
        "/api/config",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_list_canvases_empty_api(client, app_config):
    resp = await client.get("/api/canvases")
    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_get_canvas_after_direct_write(client, app_config):
    """GET /api/canvases/{name} returns whatever the server wrote."""
    # Epic #236 child 5 (#241): PUT /api/canvases/{name} is retired. Tests
    # that exercise the GET path now seed the file directly via write_canvas
    # to mimic what the write-through hook would do at runtime.
    layout = {
        "name": "main",
        "canvas_size": [3840, 2160],
        "cards": [
            {
                "type": "terminal",
                "card_id": "abc",
                "hub": "hub_1",
                "x": 100,
                "y": 100,
                "w": 720,
                "h": 480,
            }
        ],
    }
    write_canvas(app_config, "main", layout)

    resp = await client.get("/api/canvases/main")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "main"
    assert len(data["cards"]) == 1

    resp2 = await client.get("/api/canvases")
    names = await resp2.json()
    assert "main" in names


async def test_get_canvas_not_found(client, app_config):
    resp = await client.get("/api/canvases/nonexistent")
    assert resp.status == 404


async def test_put_canvas_route_removed(client, app_config):
    """Epic #236 child 5: PUT /api/canvases/{name} no longer exists."""
    resp = await client.put("/api/canvases/main", json={"cards": []})
    # aiohttp returns 405 (Method Not Allowed) for an unregistered method on
    # a known resource path.
    assert resp.status == 405


async def test_delete_canvas_via_api(client, app_config):
    """DELETE /api/canvases/{name} removes a canvas."""
    # Seed a canvas directly (server-authored snapshots are how files appear
    # post-#241; PUT /api/canvases is retired).
    write_canvas(app_config, "temp", {"name": "temp", "canvas_size": [3840, 2160], "cards": []})

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


async def test_delete_canvas_not_found_via_api(client, app_config):
    """DELETE returns 404 for non-existent canvas."""
    resp = await client.delete("/api/canvases/nonexistent")
    assert resp.status == 404


async def test_delete_default_canvas_forbidden(client, app_config):
    """DELETE returns 400 when trying to delete 'probe-qa' canvas."""
    write_canvas(app_config, "probe-qa", {"name": "probe-qa", "canvas_size": [3840, 2160], "cards": []})

    resp = await client.delete("/api/canvases/probe-qa")
    assert resp.status == 400


async def test_app_has_config_and_canvas_routes(app):
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/api/config" in routes
    assert "/api/canvases" in routes
    assert "/api/canvases/{name}" in routes
    # Epic #236 child 5 (#241): PUT on /api/canvases/{name} must NOT be
    # registered. Inspect the resource to confirm only GET + DELETE methods
    # are bound.
    methods: set[str] = set()
    for route in app.router.routes():
        if route.resource is not None and route.resource.canonical == "/api/canvases/{name}":
            methods.add(route.method)
    # aiohttp registers an implicit HEAD alongside every GET; the load-bearing
    # invariant is the absence of PUT.
    assert "PUT" not in methods
    assert {"GET", "DELETE"}.issubset(methods)
