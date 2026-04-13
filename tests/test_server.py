"""Tests for the aiohttp server routes."""

from unittest.mock import patch, AsyncMock

import pytest

from claude_rts import config
from claude_rts.server import create_app


MOCK_HUBS = [
    {"hub": "hub_1", "container": "zealous_darwin"},
    {"hub": "hub_2", "container": "suspicious_lichterman"},
]


@pytest.fixture
def app(tmp_path):
    app_config = config.load(tmp_path / ".sc")
    return create_app(app_config)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


async def test_index_returns_html(client):
    resp = await client.get("/")
    assert resp.status == 200
    text = await resp.text()
    assert "supreme-claudemander" in text
    assert "xterm" in text


async def test_hubs_endpoint_returns_json(client):
    with patch("claude_rts.server.discover_hubs", new_callable=AsyncMock, return_value=MOCK_HUBS):
        resp = await client.get("/api/hubs")

    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 2
    assert data[0]["hub"] == "hub_1"
    assert data[1]["hub"] == "hub_2"


async def test_hubs_endpoint_empty(client):
    with patch("claude_rts.server.discover_hubs", new_callable=AsyncMock, return_value=[]):
        resp = await client.get("/api/hubs")

    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_websocket_404_for_unknown_hub(client):
    # Legacy /ws/{hub} route was removed (#127); any /ws/<anything> now 404s at the router.
    resp = await client.get("/ws/anything")
    assert resp.status == 404


async def test_widget_system_info_returns_json(client):
    resp = await client.get("/api/widgets/system-info")
    assert resp.status == 200
    data = await resp.json()
    assert "hostname" in data
    assert "platform" in data
    assert "python_version" in data
    assert "uptime" in data
    assert "uptime_seconds" in data
    assert isinstance(data["uptime_seconds"], int)
    assert isinstance(data["hostname"], str)


async def test_widget_system_info_uptime_format(client):
    resp = await client.get("/api/widgets/system-info")
    assert resp.status == 200
    data = await resp.json()
    # Uptime should match "Xh Ym Zs" format
    assert "h " in data["uptime"]
    assert "m " in data["uptime"]
    assert data["uptime"].endswith("s")


async def test_app_has_all_routes(app):
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/" in routes
    assert "/api/hubs" in routes
    assert "/api/config" in routes
    assert "/api/canvases" in routes
    assert "/api/canvases/{name}" in routes
    assert "/api/startup" in routes
    assert "/api/widgets/system-info" in routes
    assert "/ws/exec" in routes
