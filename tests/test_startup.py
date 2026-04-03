"""Tests for the startup module and API endpoint."""

import json
from unittest.mock import patch, AsyncMock

import pytest

from claude_rts.startup import run_startup


async def test_discover_devcontainers_startup():
    mock_hubs = [
        {"hub": "hub_1", "container": "container_1"},
        {"hub": "hub_2", "container": "container_2"},
    ]
    with patch("claude_rts.startup.discover_hubs", new_callable=AsyncMock, return_value=mock_hubs):
        result = await run_startup("discover-devcontainers")

    assert len(result) == 2
    assert result[0]["type"] == "terminal"
    assert result[0]["name"] == "hub_1"
    assert result[0]["container"] == "container_1"
    assert "docker.exe exec" in result[0]["exec"]
    assert "container_1" in result[0]["exec"]


async def test_from_layout_startup():
    result = await run_startup("from-layout")
    assert result == []


async def test_custom_script_invalid_name():
    with pytest.raises(ValueError, match="Invalid startup script name"):
        await run_startup("../etc/passwd")


async def test_custom_script_not_found(tmp_path):
    with patch("claude_rts.startup.STARTUP_DIR", tmp_path):
        with pytest.raises(FileNotFoundError, match="not found"):
            await run_startup("nonexistent")


# ── API endpoint tests ──


@pytest.fixture
def app():
    from claude_rts.server import create_app
    return create_app()


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


async def test_startup_api_success(client):
    mock_hubs = [{"hub": "hub_1", "container": "c1"}]
    with patch("claude_rts.startup.discover_hubs", new_callable=AsyncMock, return_value=mock_hubs), \
         patch("claude_rts.server.read_config", return_value={"startup_script": "discover-devcontainers"}):
        resp = await client.get("/api/startup")

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["script"] == "discover-devcontainers"
    assert len(data["cards"]) == 1
    assert data["cards"][0]["name"] == "hub_1"


async def test_startup_api_from_layout(client):
    with patch("claude_rts.server.read_config", return_value={"startup_script": "from-layout"}):
        resp = await client.get("/api/startup")

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["cards"] == []


async def test_startup_api_error_returns_500(client):
    with patch("claude_rts.server.read_config", return_value={"startup_script": "nonexistent"}), \
         patch("claude_rts.startup.STARTUP_DIR", __import__("pathlib").Path("/tmp/empty_startup_dir_1234")):
        resp = await client.get("/api/startup")

    assert resp.status == 500
    data = await resp.json()
    assert data["status"] == "error"
    assert data["cards"] == []
