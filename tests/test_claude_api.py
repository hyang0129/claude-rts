"""Tests for the production Claude terminal control API endpoints."""

import time

import pytest
from claude_rts import config
from claude_rts.server import create_app
from claude_rts.ansi_strip import strip_ansi


# -- MockPty ---


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


# -- strip_ansi unit tests --


def test_strip_ansi_removes_csi():
    """strip_ansi removes CSI sequences (colors, cursor movement)."""
    text = "\x1b[31mhello\x1b[0m world"
    assert strip_ansi(text) == "hello world"


def test_strip_ansi_removes_osc():
    """strip_ansi removes OSC sequences (title set, etc.)."""
    text = "\x1b]0;my title\x07some text"
    assert strip_ansi(text) == "some text"


def test_strip_ansi_plain_text():
    """strip_ansi passes through plain text unchanged."""
    assert strip_ansi("hello world") == "hello world"


def test_strip_ansi_empty():
    """strip_ansi handles empty string."""
    assert strip_ansi("") == ""


def test_strip_ansi_complex():
    """strip_ansi handles mixed ANSI codes."""
    text = "\x1b[1;32m$ \x1b[0mls\r\n\x1b[34mdir1\x1b[0m  file.txt"
    assert strip_ansi(text) == "$ ls\r\ndir1  file.txt"


def test_strip_ansi_dec_private_mode():
    """strip_ansi removes DEC private mode sequences (e.g. hide cursor, alternate screen)."""
    text = "\x1b[?25lhidden cursor\x1b[?25h\x1b[?1049halt screen\x1b[?1049l"
    assert strip_ansi(text) == "hidden cursoralt screen"


# -- API endpoint tests --


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    """Create a test app with MockPty."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)

    def factory():
        app_config = config.load(tmp_path / ".sc")
        return create_app(app_config, test_mode=True)

    return factory


async def test_create_terminal(aiohttp_client, app_factory):
    """POST /api/claude/terminal/create returns descriptor with session_id."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=echo+hello")
    assert resp.status == 200
    data = await resp.json()
    assert "session_id" in data
    assert data["type"] == "terminal"
    assert data["exec"] == "echo hello"


async def test_create_terminal_with_hub_container(aiohttp_client, app_factory):
    """Create with hub and container params."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash&hub=myhub&container=mycont")
    assert resp.status == 200
    data = await resp.json()
    assert data["hub"] == "myhub"
    assert data["container"] == "mycont"


async def test_create_terminal_missing_cmd(aiohttp_client, app_factory):
    """Create without cmd returns 400."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create")
    assert resp.status == 400


async def test_create_terminal_with_layout(aiohttp_client, app_factory):
    """Create with x, y, w, h layout params."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash&x=100&y=200&w=600&h=400")
    assert resp.status == 200
    data = await resp.json()
    assert data["x"] == 100
    assert data["y"] == 200
    assert data["w"] == 600
    assert data["h"] == 400


async def test_create_terminal_interpolates_priority_credential(aiohttp_client, tmp_path, monkeypatch):
    """cmd containing ${priority_credential} is substituted with config priority_profile."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    # Write priority_profile to config
    cfg = config.read_config(app_config)
    cfg["priority_profile"] = "mykey"
    config.write_config(app_config, cfg)

    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)
    # URL-encode the ${priority_credential} placeholder
    import urllib.parse

    cmd = "claude --api-key ${priority_credential}"
    resp = await client.post(f"/api/claude/terminal/create?cmd={urllib.parse.quote(cmd)}")
    assert resp.status == 200
    data = await resp.json()
    # The card's exec field should reflect the substituted cmd
    assert data["exec"] == "claude --api-key mykey"
    assert "${priority_credential}" not in data["exec"]


async def test_create_terminal_interpolation_no_priority_profile(aiohttp_client, tmp_path, monkeypatch):
    """cmd with ${priority_credential} is left unchanged when no priority_profile is set."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    # No priority_profile set (default)
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)
    import urllib.parse

    cmd = "claude --api-key ${priority_credential}"
    resp = await client.post(f"/api/claude/terminal/create?cmd={urllib.parse.quote(cmd)}")
    assert resp.status == 200
    data = await resp.json()
    # Unchanged — placeholder still present
    assert data["exec"] == "claude --api-key ${priority_credential}"


async def test_send_to_terminal(aiohttp_client, app_factory):
    """POST /api/claude/terminal/{id}/send writes to PTY."""
    client = await aiohttp_client(app_factory())

    # Create
    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    data = await resp.json()
    sid = data["session_id"]

    # Send
    resp = await client.post(f"/api/claude/terminal/{sid}/send", data="ls -la\n")
    assert resp.status == 200
    send_data = await resp.json()
    assert send_data["status"] == "ok"
    assert send_data["sent"] == 7


async def test_send_to_nonexistent(aiohttp_client, app_factory):
    """Send to nonexistent terminal returns 404."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/nonexistent/send", data="hello")
    assert resp.status == 404


async def test_read_terminal(aiohttp_client, app_factory):
    """GET /api/claude/terminal/{id}/read returns scrollback."""
    client = await aiohttp_client(app_factory())

    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    data = await resp.json()
    sid = data["session_id"]

    resp = await client.get(f"/api/claude/terminal/{sid}/read")
    assert resp.status == 200
    read_data = await resp.json()
    assert "output" in read_data
    assert "size" in read_data
    assert "total_written" in read_data


async def test_read_terminal_strip_ansi(aiohttp_client, app_factory):
    """GET with strip_ansi=true strips ANSI codes from output."""
    client = await aiohttp_client(app_factory())

    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    data = await resp.json()
    sid = data["session_id"]

    # Inject ANSI content into scrollback directly
    mgr = client.app["session_manager"]
    session = mgr.get_session(sid)
    session.scrollback.append(b"\x1b[31mred\x1b[0m text")

    resp = await client.get(f"/api/claude/terminal/{sid}/read?strip_ansi=true")
    assert resp.status == 200
    read_data = await resp.json()
    assert "red text" in read_data["output"]
    assert "\x1b" not in read_data["output"]


async def test_read_nonexistent(aiohttp_client, app_factory):
    """Read from nonexistent terminal returns 404."""
    client = await aiohttp_client(app_factory())
    resp = await client.get("/api/claude/terminal/nonexistent/read")
    assert resp.status == 404


async def test_terminal_status(aiohttp_client, app_factory):
    """GET /api/claude/terminal/{id}/status returns metadata."""
    client = await aiohttp_client(app_factory())

    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    data = await resp.json()
    sid = data["session_id"]

    resp = await client.get(f"/api/claude/terminal/{sid}/status")
    assert resp.status == 200
    status = await resp.json()
    assert status["session_id"] == sid
    assert status["alive"] is True
    assert "age_seconds" in status
    assert "idle_seconds" in status
    assert "cmd" in status


async def test_delete_terminal(aiohttp_client, app_factory):
    """DELETE /api/claude/terminal/{id} removes from both registries."""
    client = await aiohttp_client(app_factory())

    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    data = await resp.json()
    sid = data["session_id"]

    # Verify it exists
    resp = await client.get(f"/api/claude/terminal/{sid}/status")
    assert resp.status == 200

    # Delete
    resp = await client.delete(f"/api/claude/terminal/{sid}")
    assert resp.status == 200

    # Verify gone from card registry
    card_reg = client.app["card_registry"]
    assert card_reg.get(sid) is None

    # Verify gone from session manager
    mgr = client.app["session_manager"]
    assert mgr.get_session(sid) is None


async def test_delete_nonexistent(aiohttp_client, app_factory):
    """Delete nonexistent terminal returns 404."""
    client = await aiohttp_client(app_factory())
    resp = await client.delete("/api/claude/terminal/nonexistent")
    assert resp.status == 404


async def test_list_terminals(aiohttp_client, app_factory):
    """GET /api/claude/terminals lists all terminal cards."""
    client = await aiohttp_client(app_factory())

    # Create two terminals
    resp1 = await client.post("/api/claude/terminal/create?cmd=bash")
    resp2 = await client.post("/api/claude/terminal/create?cmd=sh")
    d1 = await resp1.json()
    d2 = await resp2.json()

    resp = await client.get("/api/claude/terminals")
    assert resp.status == 200
    terminals = await resp.json()
    assert len(terminals) == 2
    sids = {t["session_id"] for t in terminals}
    assert d1["session_id"] in sids
    assert d2["session_id"] in sids
    # Each entry should have metadata
    for t in terminals:
        assert "alive" in t
        assert "age_seconds" in t


async def test_list_terminals_empty(aiohttp_client, app_factory):
    """GET /api/claude/terminals with no terminals returns empty list."""
    client = await aiohttp_client(app_factory())
    resp = await client.get("/api/claude/terminals")
    assert resp.status == 200
    assert await resp.json() == []


async def test_http_access_touches_last_client_time(aiohttp_client, app_factory):
    """HTTP read and send should update last_client_time to prevent orphan reaping."""
    client = await aiohttp_client(app_factory())

    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    data = await resp.json()
    sid = data["session_id"]

    mgr = client.app["session_manager"]
    session = mgr.get_session(sid)

    # Set last_client_time to the past
    old_time = time.monotonic() - 1000
    session.last_client_time = old_time

    # Read should touch it
    await client.get(f"/api/claude/terminal/{sid}/read")
    assert session.last_client_time > old_time

    # Reset and test send
    session.last_client_time = old_time
    await client.post(f"/api/claude/terminal/{sid}/send", data="test")
    assert session.last_client_time > old_time


async def test_create_registers_in_card_registry(aiohttp_client, app_factory):
    """POST create should register the TerminalCard in the CardRegistry."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    data = await resp.json()
    sid = data["session_id"]

    card_reg = client.app["card_registry"]
    card = card_reg.get_terminal(sid)
    assert card is not None
    assert card.session_id == sid
    assert card.alive is True


async def test_create_terminal_invalid_cols(aiohttp_client, app_factory):
    """Create with non-numeric cols returns 400."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash&cols=abc")
    assert resp.status == 400


async def test_create_terminal_invalid_layout(aiohttp_client, app_factory):
    """Create with non-numeric layout param returns 400."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash&x=abc")
    assert resp.status == 400


# ── /ws/control tests ─────────────────────────────────────────────────────


async def test_ws_control_connects(aiohttp_client, app_factory):
    """GET /ws/control upgrades to WebSocket successfully."""
    client = await aiohttp_client(app_factory())
    async with client.ws_connect("/ws/control") as ws:
        assert not ws.closed


async def test_ws_control_receives_card_created(aiohttp_client, app_factory):
    """Creating a terminal via API sends card_created over /ws/control."""

    client = await aiohttp_client(app_factory())
    async with client.ws_connect("/ws/control") as ws:
        # Create a terminal — this triggers card:registered → broadcast
        resp = await client.post("/api/claude/terminal/create?cmd=echo+hello")
        assert resp.status == 200
        data = await resp.json()
        sid = data["session_id"]

        # Read the control message (with a timeout)
        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "card_created"
        assert msg["card_id"] == sid
        assert msg["card_type"] == "terminal"
        assert "descriptor" in msg
        assert msg["descriptor"]["session_id"] == sid


async def test_ws_control_receives_card_deleted(aiohttp_client, app_factory):
    """Deleting a terminal via API sends card_deleted over /ws/control."""
    client = await aiohttp_client(app_factory())
    async with client.ws_connect("/ws/control") as ws:
        # Create
        resp = await client.post("/api/claude/terminal/create?cmd=bash")
        data = await resp.json()
        sid = data["session_id"]

        # Drain the card_created message
        await ws.receive_json(timeout=2)

        # Delete
        resp = await client.delete(f"/api/claude/terminal/{sid}")
        assert resp.status == 200

        # Read the card_deleted message
        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "card_deleted"
        assert msg["card_id"] == sid


async def test_ws_control_multiple_clients(aiohttp_client, app_factory):
    """Multiple /ws/control clients all receive the same events."""
    client = await aiohttp_client(app_factory())
    async with client.ws_connect("/ws/control") as ws1:
        async with client.ws_connect("/ws/control") as ws2:
            resp = await client.post("/api/claude/terminal/create?cmd=echo+test")
            assert resp.status == 200

            msg1 = await ws1.receive_json(timeout=2)
            msg2 = await ws2.receive_json(timeout=2)
            assert msg1["type"] == "card_created"
            assert msg2["type"] == "card_created"
            assert msg1["card_id"] == msg2["card_id"]


async def test_ws_control_descriptor_has_layout(aiohttp_client, app_factory):
    """card_created descriptor includes layout hints when provided."""
    client = await aiohttp_client(app_factory())
    async with client.ws_connect("/ws/control") as ws:
        resp = await client.post("/api/claude/terminal/create?cmd=bash&x=100&y=200&w=600&h=400")
        assert resp.status == 200

        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "card_created"
        desc = msg["descriptor"]
        assert desc["session_id"] is not None
        assert desc["x"] == 100
        assert desc["y"] == 200
        assert desc["w"] == 600
        assert desc["h"] == 400


# ── Full round-trip integration test ─────────────────────────────────────


async def test_full_lifecycle_create_send_read_delete(aiohttp_client, app_factory):
    """Integration: create terminal → send command → read output → delete.

    Exercises the complete Claude terminal control API lifecycle in sequence,
    verifying each step returns the expected state.
    """
    client = await aiohttp_client(app_factory())

    # 1. Create
    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    assert resp.status == 200
    create_data = await resp.json()
    sid = create_data["session_id"]
    assert sid
    assert create_data["type"] == "terminal"

    # Verify card is registered
    card_reg = client.app["card_registry"]
    card = card_reg.get_terminal(sid)
    assert card is not None

    # 2. Send
    resp = await client.post(f"/api/claude/terminal/{sid}/send", data="echo hello\n")
    assert resp.status == 200
    send_data = await resp.json()
    assert send_data["status"] == "ok"
    assert send_data["sent"] == len("echo hello\n")

    # 3. Read
    resp = await client.get(f"/api/claude/terminal/{sid}/read")
    assert resp.status == 200
    read_data = await resp.json()
    assert "output" in read_data
    assert "size" in read_data
    assert "total_written" in read_data

    # Verify status is still alive
    resp = await client.get(f"/api/claude/terminal/{sid}/status")
    assert resp.status == 200
    status_data = await resp.json()
    assert status_data["session_id"] == sid
    assert status_data["alive"] is True

    # 4. Delete
    resp = await client.delete(f"/api/claude/terminal/{sid}")
    assert resp.status == 200

    # Verify gone from both registries
    assert card_reg.get(sid) is None
    mgr = client.app["session_manager"]
    assert mgr.get_session(sid) is None

    # Verify status returns 404
    resp = await client.get(f"/api/claude/terminal/{sid}/status")
    assert resp.status == 404
