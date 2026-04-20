"""Tests for Container Manager API endpoints."""

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


async def test_container_discover_returns_containers(client):
    docker_output = (
        "web-app|running|node:18|Up 2 hours\n"
        "db-server|exited|postgres:15|Exited (0) 3 hours ago\n"
        "cache|running|redis:7|Up 5 hours\n"
    )
    mock_proc = _mock_docker_ps(docker_output)
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/containers/discover")

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


async def test_container_discover_empty(client):
    mock_proc = _mock_docker_ps("")
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/containers/discover")

    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_container_discover_docker_failure(client):
    mock_proc = _mock_docker_ps("", returncode=1)
    mock_proc.communicate = AsyncMock(return_value=(b"", b"Cannot connect to Docker"))
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/containers/discover")

    assert resp.status == 500
    data = await resp.json()
    assert "error" in data


async def test_container_discover_normalizes_states(client):
    docker_output = (
        "c1|running|img:1|Up 1h\n"
        "c2|created|img:2|Created\n"
        "c3|restarting|img:3|Restarting\n"
        "c4|exited|img:4|Exited\n"
        "c5|dead|img:5|Dead\n"
    )
    mock_proc = _mock_docker_ps(docker_output)
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/containers/discover")

    data = await resp.json()
    states = {c["name"]: c["state"] for c in data}
    assert states["c1"] == "online"
    assert states["c2"] == "starting"
    assert states["c3"] == "starting"
    assert states["c4"] == "offline"
    assert states["c5"] == "offline"


# ── Favorites CRUD ───────────────────────────────────────────────────────────


async def test_container_favorites_empty_by_default(client):
    resp = await client.get("/api/containers/favorites")
    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_container_favorites_put_and_get(client):
    favorites = [
        {"name": "web-app", "type": "docker", "actions": [{"label": "Terminal", "type": "terminal"}]},
        {"name": "db-server", "type": "docker", "actions": []},
    ]
    resp = await client.put(
        "/api/containers/favorites",
        json=favorites,
    )
    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 2

    # Verify persistence
    resp2 = await client.get("/api/containers/favorites")
    data2 = await resp2.json()
    assert len(data2) == 2
    assert data2[0]["name"] == "web-app"
    assert data2[1]["name"] == "db-server"


async def test_container_favorites_put_with_wrapper(client):
    """Accept favorites wrapped in {"favorites": [...]}."""
    resp = await client.put(
        "/api/containers/favorites",
        json={"favorites": [{"name": "test-container", "type": "docker"}]},
    )
    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "test-container"


async def test_container_favorites_persists_in_config(client, app):
    favorites = [{"name": "my-vm", "type": "docker"}]
    await client.put("/api/containers/favorites", json=favorites)

    # Read raw config to verify container_manager section
    app_config = app["app_config"]
    raw = config.read_config(app_config)
    assert "container_manager" in raw
    assert raw["container_manager"]["favorites"] == favorites


async def test_container_favorites_with_custom_actions(client):
    favorites = [
        {
            "name": "devcontainer-web",
            "type": "docker",
            "actions": [
                {"label": "Terminal", "type": "terminal"},
                {
                    "label": "Claude (main slot)",
                    "type": "terminal",
                    "shell_prefix": "cd /workspace/web && claude --config-dir /profiles/main",
                    "import_keys": [],
                },
            ],
        }
    ]
    resp = await client.put("/api/containers/favorites", json=favorites)
    assert resp.status == 200

    resp2 = await client.get("/api/containers/favorites")
    data = await resp2.json()
    assert len(data[0]["actions"]) == 2
    assert data[0]["actions"][1]["shell_prefix"].startswith("cd /workspace/web")
    assert data[0]["actions"][1]["shell_prefix"].endswith("/profiles/main")


# ── Start container ──────────────────────────────────────────────────────────


async def test_container_start_success(client):
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"web-app\n", b""))
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.post("/api/containers/web-app/start")

    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "web-app"
    assert data["state"] == "online"


async def test_container_start_failure(client):
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"No such container"))
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.post("/api/containers/nonexistent/start")

    assert resp.status == 500
    data = await resp.json()
    assert "error" in data


# ── Route registration ───────────────────────────────────────────────────────


# ── Stop container ──────────────────────────────────────────────────────────


async def test_container_stop_success(client):
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"web-app\n", b""))
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.post("/api/containers/web-app/stop")

    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "web-app"
    assert data["state"] == "offline"


async def test_container_stop_failure(client):
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"No such container"))
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.post("/api/containers/nonexistent/stop")

    assert resp.status == 500
    data = await resp.json()
    assert "error" in data


async def test_container_stop_test_mode(client):
    """In test mode, stop flips container state to offline."""
    # Inject test containers via the app directly
    containers = [
        {"name": "web-app", "state": "online", "image": "node:18", "status": "Up 2h"},
    ]
    app = client.app
    app["_test_containers"] = containers

    resp = await client.post("/api/containers/web-app/stop")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "web-app"
    assert data["state"] == "offline"

    # Verify mock container state was flipped
    assert containers[0]["state"] == "offline"


async def test_container_stop_test_mode_not_found(client):
    """In test mode, stop returns 500 when container name doesn't exist in mock list."""
    containers = [
        {"name": "other-container", "state": "online", "image": "ubuntu:22.04", "status": "Up 1h"},
    ]
    app = client.app
    app["_test_containers"] = containers

    resp = await client.post("/api/containers/nonexistent-container/stop")
    assert resp.status == 500
    data = await resp.json()
    assert "error" in data


async def test_container_stop_with_timeout(client):
    """Stop with optional timeout query param."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"web-app\n", b""))
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        resp = await client.post("/api/containers/web-app/stop?timeout=30")

    assert resp.status == 200
    # Verify -t flag was passed
    call_args = mock_exec.call_args[0]
    assert "-t" in call_args
    assert "30" in call_args


async def test_container_stop_invalid_timeout(client):
    """Stop with invalid timeout returns 400."""
    resp = await client.post("/api/containers/web-app/stop?timeout=invalid")
    assert resp.status == 400
    body = await resp.json()
    assert "timeout" in body["error"]


# ── created_by=canvas-claude guard (issue #200) ────────────────────────────


async def test_container_stop_guard_allows_when_label_matches(client):
    """MCP-origin stop (via=canvas-claude) succeeds when container carries
    created_by=canvas-claude label."""
    app = client.app
    app["_test_containers"] = [
        {"name": "cc-owned", "state": "online", "image": "ubuntu:24.04", "status": "Up 1m"},
    ]
    app["_test_container_labels"] = {"cc-owned": {"created_by": "canvas-claude"}}

    resp = await client.post("/api/containers/cc-owned/stop?via=canvas-claude")
    assert resp.status == 200
    data = await resp.json()
    assert data["state"] == "offline"


async def test_container_stop_guard_rejects_when_label_missing(client):
    """MCP-origin stop returns 403 not_canvas_claude_owned when the container
    has no created_by label."""
    app = client.app
    app["_test_containers"] = [
        {"name": "human-owned", "state": "online", "image": "ubuntu:24.04", "status": "Up 1h"},
    ]
    app["_test_container_labels"] = {"human-owned": {}}  # no created_by

    resp = await client.post("/api/containers/human-owned/stop?via=canvas-claude")
    assert resp.status == 403
    data = await resp.json()
    assert data["error"] == "not_canvas_claude_owned"
    assert data["container"] == "human-owned"

    # Guard must NOT have flipped state
    assert app["_test_containers"][0]["state"] == "online"


async def test_container_stop_guard_rejects_when_label_mismatched(client):
    """MCP-origin stop is rejected when created_by is set to a different value."""
    app = client.app
    app["_test_containers"] = [
        {"name": "other", "state": "online", "image": "x", "status": "Up"},
    ]
    app["_test_container_labels"] = {"other": {"created_by": "someone-else"}}

    resp = await client.post("/api/containers/other/stop?via=canvas-claude")
    assert resp.status == 403
    data = await resp.json()
    assert data["error"] == "not_canvas_claude_owned"


async def test_container_stop_human_ui_path_bypasses_guard(client):
    """UI requests (no via/header) are not guarded — humans may stop any
    container they own, including ones without the canvas-claude label."""
    app = client.app
    app["_test_containers"] = [
        {"name": "human-owned", "state": "online", "image": "x", "status": "Up"},
    ]
    app["_test_container_labels"] = {"human-owned": {}}  # no label

    resp = await client.post("/api/containers/human-owned/stop")
    assert resp.status == 200
    data = await resp.json()
    assert data["state"] == "offline"


async def test_container_stop_guard_via_spawner_header(client):
    """The X-Canvas-Claude-Spawner header is an alternate origin signal
    equivalent to ?via=canvas-claude."""
    app = client.app
    app["_test_containers"] = [
        {"name": "human-owned", "state": "online", "image": "x", "status": "Up"},
    ]
    app["_test_container_labels"] = {"human-owned": {}}

    resp = await client.post(
        "/api/containers/human-owned/stop",
        headers={"X-Canvas-Claude-Spawner": "card-abc123"},
    )
    assert resp.status == 403
    data = await resp.json()
    assert data["error"] == "not_canvas_claude_owned"


async def test_container_stop_guard_docker_inspect_failure_maps_to_500(client):
    """When docker inspect itself fails, guard returns 500 with a stable error
    key (docker_inspect_failed) rather than silently allowing or rejecting."""
    # Not setting _test_container_labels → real docker inspect path. Mock subprocess
    # to simulate docker inspect returning non-zero.
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"No such object: ghost"))
    with patch("claude_rts.server.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.post("/api/containers/ghost/stop?via=canvas-claude")

    assert resp.status == 500
    data = await resp.json()
    assert data["error"] == "docker_inspect_failed"
    assert data["container"] == "ghost"


# ── Per-container actions endpoint ──────────────────────────────────────────


async def test_container_favorites_actions_put(client):
    """PUT actions for a specific favorite container."""
    # First create a favorite
    favorites = [
        {"name": "devcontainer-web", "type": "docker", "actions": [{"label": "Terminal", "type": "terminal"}]},
    ]
    await client.put("/api/containers/favorites", json=favorites)

    # Update actions
    new_actions = [
        {"label": "Terminal", "type": "terminal"},
        {"label": "Claude", "type": "terminal", "shell_prefix": "cd /workspace && claude"},
    ]
    resp = await client.put("/api/containers/favorites/devcontainer-web/actions", json=new_actions)
    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 2
    assert data[1]["label"] == "Claude"

    # Verify persisted
    resp2 = await client.get("/api/containers/favorites")
    favs = await resp2.json()
    assert len(favs[0]["actions"]) == 2


async def test_container_favorites_actions_not_found(client):
    """PUT actions for a nonexistent favorite returns 404."""
    resp = await client.put(
        "/api/containers/favorites/nonexistent/actions",
        json=[{"label": "Terminal", "type": "terminal"}],
    )
    assert resp.status == 404
    data = await resp.json()
    assert "error" in data
    assert "nonexistent" in data["error"]


# ── Route registration ───────────────────────────────────────────────────────


async def test_container_routes_registered(app):
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/api/containers/discover" in routes
    assert "/api/containers/favorites" in routes
    assert "/api/containers/{name}/start" in routes
    assert "/api/containers/{name}/stop" in routes
    assert "/api/containers/favorites/{name}/actions" in routes
