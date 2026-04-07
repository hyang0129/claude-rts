"""Tests for the startup module and API endpoint."""

from unittest.mock import patch, AsyncMock

import pytest

from claude_rts.config import AppConfig, load
from claude_rts.startup import run_startup


@pytest.fixture
def app_config(tmp_path):
    return load(tmp_path / ".supreme-claudemander")


async def test_discover_devcontainers_startup(app_config):
    mock_hubs = [
        {"hub": "hub_1", "container": "container_1"},
        {"hub": "hub_2", "container": "container_2"},
    ]
    with patch("claude_rts.startup.discover_hubs", new_callable=AsyncMock, return_value=mock_hubs):
        result = await run_startup("discover-devcontainers", app_config)

    assert len(result) == 2
    assert result[0]["type"] == "terminal"
    assert result[0]["name"] == "hub_1"
    assert result[0]["container"] == "container_1"
    assert "docker" in result[0]["exec"] and "exec" in result[0]["exec"]
    assert "container_1" in result[0]["exec"]


async def test_from_layout_startup(app_config):
    result = await run_startup("from-layout", app_config)
    assert result == []


async def test_util_terminal_startup(app_config):
    with patch(
        "claude_rts.startup.read_config",
        return_value={"util_container": {"name": "my-util"}},
    ):
        result = await run_startup("util-terminal", app_config)

    assert len(result) == 1
    assert result[0]["type"] == "terminal"
    assert result[0]["name"] == "my-util"
    assert result[0]["container"] == "my-util"
    assert "docker" in result[0]["exec"] and "exec" in result[0]["exec"]
    assert "my-util" in result[0]["exec"]


async def test_util_terminal_startup_default_name(app_config):
    with patch("claude_rts.startup.read_config", return_value={}):
        result = await run_startup("util-terminal", app_config)

    assert result[0]["container"] == "supreme-claudemander-util"


async def test_custom_script_invalid_name(app_config):
    with pytest.raises(ValueError, match="Invalid startup script name"):
        await run_startup("../etc/passwd", app_config)


async def test_custom_script_not_found(tmp_path):
    """Custom script not found in startup dir."""
    ac = AppConfig(config_dir=tmp_path)
    # ensure the startup dir exists but is empty
    (tmp_path / "startup").mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileNotFoundError, match="not found"):
        await run_startup("nonexistent", ac)


# ── API endpoint tests ──


@pytest.fixture
def app(app_config):
    from claude_rts.server import create_app

    return create_app(app_config)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


async def test_startup_api_success(client):
    mock_hubs = [{"hub": "hub_1", "container": "c1"}]
    with (
        patch("claude_rts.startup.discover_hubs", new_callable=AsyncMock, return_value=mock_hubs),
        patch("claude_rts.server.read_config", return_value={"startup_script": "discover-devcontainers"}),
    ):
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


async def test_startup_api_error_returns_500(client, tmp_path):
    with (
        patch("claude_rts.server.read_config", return_value={"startup_script": "nonexistent"}),
        patch("claude_rts.startup.run_startup", new_callable=AsyncMock, side_effect=FileNotFoundError("not found")),
    ):
        resp = await client.get("/api/startup")

    assert resp.status == 500
    data = await resp.json()
    assert data["status"] == "error"
    assert data["cards"] == []
