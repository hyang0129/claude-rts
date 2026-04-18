"""Tests for the Canvas Claude Code API endpoints."""

import time

import pytest

from claude_rts import config
from claude_rts.server import create_app


class MockPty:
    """Mock PtyProcess for testing."""

    def __init__(self):
        self._alive = True
        self._written = []

    def isalive(self):
        return self._alive

    def read(self):
        time.sleep(0.1)
        if not self._alive:
            raise EOFError()
        return ""

    def write(self, text):
        self._written.append(text)

    def setwinsize(self, rows, cols):
        pass

    def terminate(self, force=False):
        self._alive = False

    @classmethod
    def spawn(cls, cmd, dimensions=(24, 80)):
        return cls()


def _mock_subprocess_run(*a, **kw):
    return type("R", (), {"returncode": 0, "stderr": b""})()


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    """Create a test app with MockPty and subprocess mocked."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run)

    def factory():
        app_config = config.load(tmp_path / ".sc")
        return create_app(app_config, test_mode=True)

    return factory


async def test_canvas_claude_create(aiohttp_client, app_factory):
    """POST /api/canvas-claude/create returns descriptor with session_id and type."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/canvas-claude/create?container=test-container&profile=test-profile")
    assert resp.status == 200
    data = await resp.json()
    assert "session_id" in data
    assert data["type"] == "canvas_claude"


async def test_canvas_claude_create_with_profile(aiohttp_client, app_factory):
    """Create with a profile sets profile in the descriptor."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/canvas-claude/create?container=test-container&profile=my-profile")
    assert resp.status == 200
    data = await resp.json()
    assert data.get("profile") == "my-profile"


async def test_canvas_claude_create_bad_layout(aiohttp_client, app_factory):
    """Non-integer layout params return 400."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/canvas-claude/create?container=test-container&profile=test-profile&x=notanint")
    assert resp.status == 400


async def test_canvas_claude_new_session_not_found(aiohttp_client, app_factory):
    """POST /api/canvas-claude/nonexistent/new-session returns 404."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/canvas-claude/nonexistent/new-session")
    assert resp.status == 404


async def test_canvas_claude_clear_not_found(aiohttp_client, app_factory):
    """POST /api/canvas-claude/nonexistent/clear returns 404."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/canvas-claude/nonexistent/clear")
    assert resp.status == 404


async def test_canvas_claude_new_session(aiohttp_client, app_factory):
    """POST /api/canvas-claude/{id}/new-session returns new session_id."""
    client = await aiohttp_client(app_factory())
    # Create a card first
    resp = await client.post("/api/canvas-claude/create?container=test-container&profile=test-profile")
    assert resp.status == 200
    data = await resp.json()
    old_sid = data["session_id"]

    # Restart session
    resp = await client.post(f"/api/canvas-claude/{old_sid}/new-session")
    assert resp.status == 200
    result = await resp.json()
    assert result["status"] == "ok"
    assert "session_id" in result
    # New session gets a fresh session_id
    assert result["session_id"] != old_sid


async def test_canvas_claude_clear(aiohttp_client, app_factory):
    """POST /api/canvas-claude/{id}/clear returns ok."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/canvas-claude/create?container=test-container&profile=test-profile")
    assert resp.status == 200
    sid = (await resp.json())["session_id"]

    resp = await client.post(f"/api/canvas-claude/{sid}/clear")
    assert resp.status == 200
    result = await resp.json()
    assert result["status"] == "ok"


async def test_canvas_claude_create_defaults_to_main_profile(aiohttp_client, app_factory):
    """POST /api/canvas-claude/create without profile defaults to the main slot (#163).

    Previously this returned 400 when no priority_profile was configured. The
    main slot always has a valid name (default 'main'), so the fallback now
    succeeds unconditionally — credential absence is surfaced later by the
    card's retry overlay, not by the create endpoint.
    """
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/canvas-claude/create?container=test-container")
    assert resp.status == 200
    data = await resp.json()
    assert data.get("profile") == "main"


async def test_canvas_claude_create_uses_configured_main_profile_name(aiohttp_client, tmp_path, monkeypatch):
    """The configurable main_profile_name in config.json overrides the 'main' default."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run)

    from claude_rts import config as cfg_module
    from claude_rts.server import create_app

    app_config = cfg_module.load(tmp_path / ".sc")
    cfg_module.write_config(app_config, {"main_profile_name": "custom-slot"})
    app = create_app(app_config, test_mode=True)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/canvas-claude/create?container=test-container")
        assert resp.status == 200
        data = await resp.json()
        assert data.get("profile") == "custom-slot"
