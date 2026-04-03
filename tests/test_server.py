"""Tests for the aiohttp server routes."""

import json
from unittest.mock import patch, AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from claude_rts.server import create_app


MOCK_HUBS = [
    {"hub": "hub_1", "container": "zealous_darwin"},
    {"hub": "hub_2", "container": "suspicious_lichterman"},
]


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


async def test_index_returns_html(client):
    resp = await client.get("/")
    assert resp.status == 200
    text = await resp.text()
    assert "claude-rts" in text
    assert "xterm" in text


async def test_hubs_endpoint_returns_json(client):
    with patch("claude_rts.server.discover_hubs", new_callable=AsyncMock,
               return_value=MOCK_HUBS):
        resp = await client.get("/api/hubs")

    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 2
    assert data[0]["hub"] == "hub_1"
    assert data[1]["hub"] == "hub_2"


async def test_hubs_endpoint_empty(client):
    with patch("claude_rts.server.discover_hubs", new_callable=AsyncMock,
               return_value=[]):
        resp = await client.get("/api/hubs")

    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_websocket_404_for_unknown_hub(client):
    with patch("claude_rts.server.discover_hubs", new_callable=AsyncMock,
               return_value=MOCK_HUBS):
        resp = await client.get("/ws/nonexistent_hub")
    assert resp.status == 404


async def test_app_has_all_routes(app):
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, 'resource')]
    assert "/" in routes
    assert "/api/hubs" in routes
    assert "/api/config" in routes
    assert "/api/canvases" in routes
    assert "/api/canvases/{name}" in routes
    assert "/api/startup" in routes
    assert "/ws/exec" in routes
    assert "/ws/{hub}" in routes
