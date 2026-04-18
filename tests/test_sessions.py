"""Tests for session persistence: ScrollbackBuffer, SessionManager, and session APIs."""

import time
from unittest.mock import patch, AsyncMock

import pytest

from claude_rts.sessions import ScrollbackBuffer, SessionManager, _valid_container_name
from claude_rts import config
from claude_rts.server import create_app


# ── ScrollbackBuffer unit tests ─────────────────────────────────────────────


def test_scrollback_empty():
    buf = ScrollbackBuffer(1024)
    assert buf.get_all() == b""
    assert buf.size == 0
    assert buf.total_written == 0


def test_scrollback_basic_write():
    buf = ScrollbackBuffer(1024)
    buf.append(b"hello")
    assert buf.get_all() == b"hello"
    assert buf.size == 5
    assert buf.total_written == 5


def test_scrollback_multiple_writes():
    buf = ScrollbackBuffer(1024)
    buf.append(b"hello ")
    buf.append(b"world")
    assert buf.get_all() == b"hello world"
    assert buf.total_written == 11


def test_scrollback_wraparound():
    buf = ScrollbackBuffer(10)
    buf.append(b"12345")
    buf.append(b"67890")
    assert buf.get_all() == b"1234567890"
    # Now overflow
    buf.append(b"ABC")
    result = buf.get_all()
    assert len(result) == 10
    assert result == b"4567890ABC"


def test_scrollback_large_write_exceeds_capacity():
    buf = ScrollbackBuffer(8)
    buf.append(b"0123456789ABCDEF")
    # Only last 8 bytes kept
    assert buf.get_all() == b"89ABCDEF"
    assert buf.size == 8


def test_scrollback_empty_append():
    buf = ScrollbackBuffer(1024)
    buf.append(b"data")
    buf.append(b"")
    assert buf.get_all() == b"data"


# ── SessionManager unit tests (mocked PTY) ──────────────────────────────────


class MockPty:
    """Mock PtyProcess for testing."""

    def __init__(self):
        self._alive = True
        self._output_queue = []
        self._written = []

    def isalive(self):
        return self._alive

    def read(self):
        if self._output_queue:
            return self._output_queue.pop(0)
        # Simulate blocking read that returns when killed
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


async def test_session_manager_create(monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    session = mgr.create_session("echo hello")
    assert session.session_id
    assert session.cmd == "echo hello"
    assert session.alive
    assert mgr.get_session(session.session_id) is session
    mgr.stop_all()


async def test_session_id_format(monkeypatch):
    """Session IDs should use rts-{hex8} format."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    session = mgr.create_session("test")
    assert session.session_id.startswith("rts-")
    assert len(session.session_id) == 12  # "rts-" + 8 hex chars
    mgr.stop_all()


async def test_create_session_with_container_tmux(monkeypatch):
    """When container is provided and tmux is cached, should use tmux command."""
    spawned_cmds = []
    original_spawn = MockPty.spawn

    @classmethod
    def tracking_spawn(cls, cmd, dimensions=(24, 80)):
        spawned_cmds.append(cmd)
        return original_spawn(cmd, dimensions)

    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    MockPty.spawn = tracking_spawn
    try:
        mgr = SessionManager()
        mgr._tmux_cache["my-container"] = True
        session = mgr.create_session("bash -l", hub="hub1", container="my-container")
        assert any("tmux new-session" in c for c in spawned_cmds)
        assert session.container == "my-container"
        mgr.stop_all()
    finally:
        MockPty.spawn = original_spawn


async def test_create_session_fallback_no_tmux(monkeypatch):
    """Without tmux in cache but with container, should wrap cmd in docker exec."""
    spawned_cmds = []
    original_spawn = MockPty.spawn

    @classmethod
    def tracking_spawn(cls, cmd, dimensions=(24, 80)):
        spawned_cmds.append(cmd)
        return original_spawn(cmd, dimensions)

    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    MockPty.spawn = tracking_spawn
    try:
        mgr = SessionManager()
        # No tmux cache entry — should still docker-exec into the container
        session = mgr.create_session("bash", hub="hub1", container="my-container")
        assert "docker" in spawned_cmds[-1]
        assert "exec" in spawned_cmds[-1]
        assert "my-container" in spawned_cmds[-1]
        assert "bash" in spawned_cmds[-1]
        assert session.container == "my-container"
        # cmd metadata unchanged
        assert session.cmd == "bash"
        mgr.stop_all()
    finally:
        MockPty.spawn = original_spawn


async def test_create_session_no_tmux_no_double_wrap(monkeypatch):
    """When cmd already starts with docker, don't double-wrap even if container is set."""
    spawned_cmds = []
    original_spawn = MockPty.spawn

    @classmethod
    def tracking_spawn(cls, cmd, dimensions=(24, 80)):
        spawned_cmds.append(cmd)
        return original_spawn(cmd, dimensions)

    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    MockPty.spawn = tracking_spawn
    try:
        mgr = SessionManager()
        full_cmd = "docker exec -it my-container bash -l"
        mgr.create_session(full_cmd, container="my-container")
        assert spawned_cmds[-1] == full_cmd
        mgr.stop_all()
    finally:
        MockPty.spawn = original_spawn


async def test_destroy_session_default_no_kill_tmux(monkeypatch):
    """Default destroy_session should not attempt to kill tmux."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    session = mgr.create_session("test", container="cont1")
    sid = session.session_id
    # destroy without kill_tmux — should not raise or call docker
    mgr.destroy_session(sid, kill_tmux=False)
    assert mgr.get_session(sid) is None
    mgr.stop_all()


async def test_stop_all_preserves_tmux(monkeypatch):
    """stop_all should detach sessions without killing tmux."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    s1 = mgr.create_session("cmd1", container="cont1")
    s2 = mgr.create_session("cmd2", container="cont2")
    mgr.stop_all()
    assert mgr.get_session(s1.session_id) is None
    assert mgr.get_session(s2.session_id) is None


async def test_session_manager_destroy(monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    session = mgr.create_session("test")
    sid = session.session_id
    mgr.destroy_session(sid)
    assert mgr.get_session(sid) is None
    assert not session.alive
    mgr.stop_all()


async def test_session_manager_list(monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    mgr.create_session("cmd1", hub="hub1")
    mgr.create_session("cmd2", hub="hub2")
    sessions = mgr.list_sessions()
    assert len(sessions) == 2
    hubs = {s["hub"] for s in sessions}
    assert hubs == {"hub1", "hub2"}
    mgr.stop_all()


async def test_session_manager_stop_all(monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    s1 = mgr.create_session("cmd1")
    s2 = mgr.create_session("cmd2")
    mgr.stop_all()
    assert mgr.get_session(s1.session_id) is None
    assert mgr.get_session(s2.session_id) is None


# ── Test puppeting API tests ─────────────────────────────────────────────────


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    return create_app(app_config, test_mode=True)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


async def test_test_session_create(client):
    resp = await client.post("/api/test/session/create?cmd=echo+hello")
    assert resp.status == 200
    data = await resp.json()
    assert "session_id" in data


async def test_test_session_status(client):
    resp = await client.post("/api/test/session/create?cmd=echo+hello")
    data = await resp.json()
    sid = data["session_id"]

    resp = await client.get(f"/api/test/session/{sid}/status")
    assert resp.status == 200
    status = await resp.json()
    assert status["session_id"] == sid
    assert status["alive"] is True
    assert status["client_count"] == 0


async def test_test_session_send_and_read(client):
    resp = await client.post("/api/test/session/create?cmd=echo+hello")
    data = await resp.json()
    sid = data["session_id"]

    # Send text to PTY
    resp = await client.post(f"/api/test/session/{sid}/send", data="test input")
    assert resp.status == 200

    # Read scrollback (may be empty since MockPty doesn't echo)
    resp = await client.get(f"/api/test/session/{sid}/read")
    assert resp.status == 200
    read_data = await resp.json()
    assert "output" in read_data
    assert "size" in read_data


async def test_test_session_delete(client):
    resp = await client.post("/api/test/session/create?cmd=echo+hello")
    data = await resp.json()
    sid = data["session_id"]

    resp = await client.delete(f"/api/test/session/{sid}")
    assert resp.status == 200

    resp = await client.get(f"/api/test/session/{sid}/status")
    assert resp.status == 404


async def test_test_session_not_found(client):
    resp = await client.get("/api/test/session/nonexistent/status")
    assert resp.status == 404


async def test_test_sessions_list(client):
    await client.post("/api/test/session/create?cmd=cmd1")
    await client.post("/api/test/session/create?cmd=cmd2")
    resp = await client.get("/api/test/sessions")
    assert resp.status == 200
    data = await resp.json()
    assert len(data) >= 2


async def test_sessions_list_api(client):
    """The non-test sessions list endpoint should also work."""
    resp = await client.get("/api/sessions")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, list)


async def test_test_mode_disabled(tmp_path):
    """Test API should NOT be available when test_mode=False."""
    with patch("claude_rts.sessions.PtyProcess", MockPty):
        app = create_app(config.load(tmp_path / ".sc"), test_mode=False)
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/api/test/sessions" not in routes
    assert "/api/test/session/{id}/read" not in routes


async def test_app_has_session_routes(tmp_path):
    """Verify session WebSocket routes are registered."""
    with patch("claude_rts.sessions.PtyProcess", MockPty):
        app = create_app(config.load(tmp_path / ".sc"), test_mode=False)
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/ws/session/new" in routes
    assert "/ws/session/{session_id}" in routes
    assert "/api/sessions" in routes


# ── Container name validation tests ────────────────────────────────────────


def test_valid_container_names():
    assert _valid_container_name("my-container")
    assert _valid_container_name("container_1")
    assert _valid_container_name("abc.def")
    assert _valid_container_name("a")


def test_invalid_container_names():
    assert not _valid_container_name("")
    assert not _valid_container_name("-starts-with-dash")
    assert not _valid_container_name(".starts-with-dot")
    assert not _valid_container_name("has spaces")
    assert not _valid_container_name("semi;colon")
    assert not _valid_container_name("$(injection)")
    assert not _valid_container_name("foo; rm -rf /")
    assert not _valid_container_name("a" * 129)


async def test_create_session_rejects_invalid_container(monkeypatch):
    """Invalid container names should be sanitized to None."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    mgr._tmux_cache["$(evil)"] = True  # even if cached, should be rejected
    session = mgr.create_session("bash -l", container="$(evil)")
    assert session.container is None
    mgr.stop_all()


# ── tmux recovery test ─────────────────────────────────────────────────────


async def test_recover_tmux_sessions(monkeypatch):
    """recover_tmux_sessions should find and re-attach to existing tmux sessions."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)

    # Mock asyncio.create_subprocess_exec to simulate tmux list-sessions
    call_count = {"list": 0, "capture": 0}

    async def mock_subprocess_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        cmd_args = list(args)
        if "list-sessions" in cmd_args:
            call_count["list"] += 1
            mock_proc.communicate.return_value = (b"rts-abc12345\nother-session\n", b"")
            mock_proc.returncode = 0
        elif "capture-pane" in cmd_args:
            call_count["capture"] += 1
            mock_proc.communicate.return_value = (b"$ previous output\n", b"")
            mock_proc.returncode = 0
        else:
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 1
        return mock_proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", mock_subprocess_exec)

    mgr = SessionManager()
    hubs = [{"container": "test-container", "hub": "hub1"}]
    recovered = await mgr.recover_tmux_sessions(hubs)

    assert recovered == 1
    assert "rts-abc12345" in mgr._sessions
    session = mgr._sessions["rts-abc12345"]
    assert session.hub == "hub1"
    assert session.container == "test-container"
    assert mgr._tmux_cache["test-container"] is True
    # Scrollback should be seeded from capture-pane
    assert session.scrollback.size > 0
    assert call_count["list"] == 1
    assert call_count["capture"] == 1
    mgr.stop_all()


async def test_recover_skips_non_rts_sessions(monkeypatch):
    """recover_tmux_sessions should skip sessions without rts- prefix."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)

    async def mock_subprocess_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        cmd_args = list(args)
        if "list-sessions" in cmd_args:
            mock_proc.communicate.return_value = (b"user-session\nanother\n", b"")
            mock_proc.returncode = 0
        else:
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 1
        return mock_proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", mock_subprocess_exec)

    mgr = SessionManager()
    recovered = await mgr.recover_tmux_sessions([{"container": "c1", "hub": "h1"}])
    assert recovered == 0
    assert len(mgr._sessions) == 0
    mgr.stop_all()


async def test_tmux_disabled_via_config(monkeypatch):
    """When tmux_enabled=False, sessions should not use tmux even if cached.
    The cmd is still wrapped in docker exec when a container is specified."""
    spawned_cmds = []
    original_spawn = MockPty.spawn

    @classmethod
    def tracking_spawn(cls, cmd, dimensions=(24, 80)):
        spawned_cmds.append(cmd)
        return original_spawn(cmd, dimensions)

    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    MockPty.spawn = tracking_spawn
    try:
        mgr = SessionManager(tmux_enabled=False)
        mgr._tmux_cache["my-container"] = True
        mgr.create_session("bash -l", container="my-container")
        # Should NOT use tmux, but still exec into the container
        assert "tmux" not in spawned_cmds[-1]
        assert "docker" in spawned_cmds[-1]
        assert "exec" in spawned_cmds[-1]
        assert "my-container" in spawned_cmds[-1]
        assert "bash -l" in spawned_cmds[-1]
        mgr.stop_all()
    finally:
        MockPty.spawn = original_spawn
