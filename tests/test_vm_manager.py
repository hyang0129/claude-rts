"""Tests for VM Manager API endpoints."""

from unittest.mock import patch, AsyncMock

import pytest

from claude_rts import config
from claude_rts.server import create_app


@pytest.fixture
def app(tmp_path):
    app_config = config.load(tmp_path / ".sc")
    return create_app(app_config)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


# ── Discovery ────────────────────────────────────────────────────────────────


def _mock_docker_ps(stdout_text, returncode=0):
    """Create a mock for asyncio.create_subprocess_exec returning docker ps output."""
    mock_proc = AsyncMock()
    mock_proc.returncode = returncode
    mock_proc.communicate = AsyncMock(return_value=(stdout_text.encode(), b""))
    return mock_proc


async def test_vm_discover_returns_containers(client):
    docker_output = (
        "web-app|running|node:18|Up 2 hours\n"
        "db-server|exited|postgres:15|Exited (0) 3 hours ago\n"
        "cache|running|redis:7|Up 5 hours\n"
    )
    mock_proc = _mock_docker_ps(docker_output)
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/vms/discover")

    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 3
    # Sorted by name
    assert data[0]["name"] == "cache"
    assert data[0]["state"] == "online"
    assert data[1]["name"] == "db-server"
    assert data[1]["state"] == "offline"
    assert data[2]["name"] == "web-app"
    assert data[2]["state"] == "online"


async def test_vm_discover_empty(client):
    mock_proc = _mock_docker_ps("")
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/vms/discover")

    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_vm_discover_docker_failure(client):
    mock_proc = _mock_docker_ps("", returncode=1)
    mock_proc.communicate = AsyncMock(return_value=(b"", b"Cannot connect to Docker"))
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/vms/discover")

    assert resp.status == 500
    data = await resp.json()
    assert "error" in data


async def test_vm_discover_normalizes_states(client):
    docker_output = (
        "c1|running|img:1|Up 1h\n"
        "c2|created|img:2|Created\n"
        "c3|restarting|img:3|Restarting\n"
        "c4|exited|img:4|Exited\n"
        "c5|dead|img:5|Dead\n"
    )
    mock_proc = _mock_docker_ps(docker_output)
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/vms/discover")

    data = await resp.json()
    states = {c["name"]: c["state"] for c in data}
    assert states["c1"] == "online"
    assert states["c2"] == "starting"
    assert states["c3"] == "starting"
    assert states["c4"] == "offline"
    assert states["c5"] == "offline"


# ── Favorites CRUD ───────────────────────────────────────────────────────────


async def test_vm_favorites_empty_by_default(client):
    resp = await client.get("/api/vms/favorites")
    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_vm_favorites_put_and_get(client):
    favorites = [
        {"name": "web-app", "type": "docker", "actions": [{"label": "Terminal", "type": "terminal"}]},
        {"name": "db-server", "type": "docker", "actions": []},
    ]
    resp = await client.put(
        "/api/vms/favorites",
        json=favorites,
    )
    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 2

    # Verify persistence
    resp2 = await client.get("/api/vms/favorites")
    data2 = await resp2.json()
    assert len(data2) == 2
    assert data2[0]["name"] == "web-app"
    assert data2[1]["name"] == "db-server"


async def test_vm_favorites_put_with_wrapper(client):
    """Accept favorites wrapped in {"favorites": [...]}."""
    resp = await client.put(
        "/api/vms/favorites",
        json={"favorites": [{"name": "test-container", "type": "docker"}]},
    )
    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "test-container"


async def test_vm_favorites_persists_in_config(client, app):
    favorites = [{"name": "my-vm", "type": "docker"}]
    await client.put("/api/vms/favorites", json=favorites)

    # Read raw config to verify vm_manager section
    app_config = app["app_config"]
    raw = config.read_config(app_config)
    assert "vm_manager" in raw
    assert raw["vm_manager"]["favorites"] == favorites


async def test_vm_favorites_with_custom_actions(client):
    favorites = [
        {
            "name": "devcontainer-web",
            "type": "docker",
            "actions": [
                {"label": "Terminal", "type": "terminal"},
                {
                    "label": "Claude (web creds)",
                    "type": "terminal",
                    "shell_prefix": "cd /workspace/web && claude --config-dir ${priority_credential}",
                    "import_keys": ["priority_credential"],
                },
            ],
        }
    ]
    resp = await client.put("/api/vms/favorites", json=favorites)
    assert resp.status == 200

    resp2 = await client.get("/api/vms/favorites")
    data = await resp2.json()
    assert len(data[0]["actions"]) == 2
    assert data[0]["actions"][1]["shell_prefix"].startswith("cd /workspace/web")
    assert "priority_credential" in data[0]["actions"][1]["import_keys"]


# ── Start container ──────────────────────────────────────────────────────────


async def test_vm_start_success(client):
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"web-app\n", b""))
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.post("/api/vms/web-app/start")

    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "web-app"
    assert data["state"] == "online"


async def test_vm_start_failure(client):
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"No such container"))
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.post("/api/vms/nonexistent/start")

    assert resp.status == 500
    data = await resp.json()
    assert "error" in data


# ── Route registration ───────────────────────────────────────────────────────


async def test_vm_routes_registered(app):
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/api/vms/discover" in routes
    assert "/api/vms/favorites" in routes
    assert "/api/vms/{name}/start" in routes
