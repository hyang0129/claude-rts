"""Tests for the production Claude terminal control API endpoints."""

import asyncio
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


async def test_create_terminal_passes_cmd_through_unchanged(aiohttp_client, tmp_path, monkeypatch):
    """cmd is passed through verbatim — no ${priority_credential} substitution (issue #163).

    The former server-side placeholder was removed. Any literal placeholder
    string in cmd must appear unchanged in the resulting card's exec field.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")

    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)
    import urllib.parse

    cmd = "claude --api-key ${priority_credential}"
    resp = await client.post(f"/api/claude/terminal/create?cmd={urllib.parse.quote(cmd)}")
    assert resp.status == 200
    data = await resp.json()
    # Verbatim — no substitution, no warning, no change.
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


# ── Issue #151: rename, recovery-script, list_terminals with new fields ───


async def test_rename_terminal(aiohttp_client, app_factory):
    """PUT /api/claude/terminal/{id}/rename sets display_name."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    data = await resp.json()
    sid = data["session_id"]

    resp = await client.put(
        f"/api/claude/terminal/{sid}/rename",
        json={"display_name": "Build Server"},
    )
    assert resp.status == 200
    result = await resp.json()
    assert result["display_name"] == "Build Server"

    # Verify via card registry
    card = client.app["card_registry"].get_terminal(sid)
    assert card.display_name == "Build Server"


async def test_rename_terminal_nonexistent(aiohttp_client, app_factory):
    """PUT rename on nonexistent terminal returns 404."""
    client = await aiohttp_client(app_factory())
    resp = await client.put(
        "/api/claude/terminal/nonexistent/rename",
        json={"display_name": "Test"},
    )
    assert resp.status == 404


async def test_recovery_script_get_put(aiohttp_client, app_factory):
    """GET/PUT /api/claude/terminal/{id}/recovery-script round-trips."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    data = await resp.json()
    sid = data["session_id"]

    # Initially empty
    resp = await client.get(f"/api/claude/terminal/{sid}/recovery-script")
    assert resp.status == 200
    result = await resp.json()
    assert result["recovery_script"] == ""

    # Set it
    resp = await client.put(
        f"/api/claude/terminal/{sid}/recovery-script",
        json={"recovery_script": "cd /work && make dev"},
    )
    assert resp.status == 200
    result = await resp.json()
    assert result["recovery_script"] == "cd /work && make dev"

    # Read it back
    resp = await client.get(f"/api/claude/terminal/{sid}/recovery-script")
    assert resp.status == 200
    result = await resp.json()
    assert result["recovery_script"] == "cd /work && make dev"


async def test_recovery_script_nonexistent(aiohttp_client, app_factory):
    """GET/PUT recovery-script on nonexistent terminal returns 404."""
    client = await aiohttp_client(app_factory())
    resp = await client.get("/api/claude/terminal/nonexistent/recovery-script")
    assert resp.status == 404

    resp = await client.put(
        "/api/claude/terminal/nonexistent/recovery-script",
        json={"recovery_script": "test"},
    )
    assert resp.status == 404


async def test_list_terminals_includes_display_name_and_recovery(aiohttp_client, app_factory):
    """GET /api/claude/terminals includes display_name and recovery_script."""
    client = await aiohttp_client(app_factory())

    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    data = await resp.json()
    sid = data["session_id"]

    # Set display_name and recovery_script
    await client.put(
        f"/api/claude/terminal/{sid}/rename",
        json={"display_name": "My Terminal"},
    )
    await client.put(
        f"/api/claude/terminal/{sid}/recovery-script",
        json={"recovery_script": "echo hello"},
    )

    resp = await client.get("/api/claude/terminals")
    assert resp.status == 200
    terminals = await resp.json()
    assert len(terminals) == 1
    t = terminals[0]
    assert t["display_name"] == "My Terminal"
    assert t["recovery_script"] == "echo hello"


async def test_ws_control_receives_card_updated_on_rename(aiohttp_client, app_factory):
    """Renaming a terminal sends card_updated over /ws/control."""
    client = await aiohttp_client(app_factory())
    async with client.ws_connect("/ws/control") as ws:
        resp = await client.post("/api/claude/terminal/create?cmd=bash")
        data = await resp.json()
        sid = data["session_id"]

        # Drain card_created
        await ws.receive_json(timeout=2)

        # Rename
        await client.put(
            f"/api/claude/terminal/{sid}/rename",
            json={"display_name": "Renamed"},
        )

        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "card_updated"
        assert msg["card_id"] == sid
        assert msg["display_name"] == "Renamed"


# ── Issue #238: generic PUT /api/cards/{id}/state endpoint ────────────────


async def test_cards_state_put_patches_display_name_and_broadcasts(aiohttp_client, app_factory):
    """PUT /api/cards/{id}/state applies partial dict, updates registry, broadcasts card_updated."""
    client = await aiohttp_client(app_factory())
    async with client.ws_connect("/ws/control") as ws:
        resp = await client.post("/api/claude/terminal/create?cmd=bash")
        data = await resp.json()
        sid = data["session_id"]

        # Drain card_created
        await ws.receive_json(timeout=2)

        resp = await client.put(
            f"/api/cards/{sid}/state",
            json={"display_name": "Renamed via generic"},
        )
        assert resp.status == 200
        result = await resp.json()
        assert result["status"] == "ok"
        assert result["display_name"] == "Renamed via generic"

        # CardRegistry has been mutated
        card = client.app["card_registry"].get_terminal(sid)
        assert card.display_name == "Renamed via generic"

        # card_updated was broadcast
        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "card_updated"
        assert msg["card_id"] == sid
        assert msg["display_name"] == "Renamed via generic"


async def test_cards_state_put_multiple_fields(aiohttp_client, app_factory):
    """PUT /api/cards/{id}/state accepts multiple fields in one patch."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    sid = (await resp.json())["session_id"]

    resp = await client.put(
        f"/api/cards/{sid}/state",
        json={"display_name": "DN", "recovery_script": "echo hi"},
    )
    assert resp.status == 200
    result = await resp.json()
    assert result["display_name"] == "DN"
    assert result["recovery_script"] == "echo hi"

    card = client.app["card_registry"].get_terminal(sid)
    assert card.display_name == "DN"
    assert card.recovery_script == "echo hi"


async def test_cards_state_put_rejects_unknown_field(aiohttp_client, app_factory):
    """PUT /api/cards/{id}/state returns 400 for fields outside the allowlist."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    sid = (await resp.json())["session_id"]

    resp = await client.put(
        f"/api/cards/{sid}/state",
        json={"bogus_field": 1},
    )
    assert resp.status == 400
    text = await resp.text()
    assert "bogus_field" in text


async def test_cards_state_put_rejects_non_string_value(aiohttp_client, app_factory):
    """Allowlisted str fields (e.g. ``display_name``) reject non-string values with 400.

    After child 3 (#239), per-field type validation is declared via
    ``MUTABLE_FIELD_TYPES``; fields defaulting to ``str`` still reject ints.
    """
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    sid = (await resp.json())["session_id"]

    resp = await client.put(
        f"/api/cards/{sid}/state",
        json={"display_name": 123},
    )
    assert resp.status == 400


async def test_cards_state_put_nonexistent_card(aiohttp_client, app_factory):
    """PUT /api/cards/{id}/state on unknown card returns 404."""
    client = await aiohttp_client(app_factory())
    resp = await client.put(
        "/api/cards/nonexistent/state",
        json={"display_name": "x"},
    )
    assert resp.status == 404


async def test_cards_state_put_rejects_non_object_body(aiohttp_client, app_factory):
    """PUT /api/cards/{id}/state with a non-object JSON body returns 400."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    sid = (await resp.json())["session_id"]

    resp = await client.put(
        f"/api/cards/{sid}/state",
        data="[1, 2, 3]",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_cards_state_put_patches_starred_and_broadcasts(aiohttp_client, app_factory):
    """Epic #236 child 3 (#239): PUT /api/cards/{id}/state accepts bool ``starred``.

    Mutates CardRegistry, broadcasts ``card_updated`` with the new starred
    value, and surfaces it in ``to_descriptor`` for the boot path.
    """
    client = await aiohttp_client(app_factory())
    async with client.ws_connect("/ws/control") as ws:
        resp = await client.post("/api/claude/terminal/create?cmd=bash")
        sid = (await resp.json())["session_id"]
        await ws.receive_json(timeout=2)  # drain card_created

        resp = await client.put(f"/api/cards/{sid}/state", json={"starred": True})
        assert resp.status == 200
        result = await resp.json()
        assert result["status"] == "ok"
        assert result["starred"] is True

        card = client.app["card_registry"].get_terminal(sid)
        assert card.starred is True
        assert card.to_descriptor()["starred"] is True

        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "card_updated"
        assert msg["card_id"] == sid
        assert msg["starred"] is True

        # Toggle back off.
        resp = await client.put(f"/api/cards/{sid}/state", json={"starred": False})
        assert resp.status == 200
        assert client.app["card_registry"].get_terminal(sid).starred is False
        msg = await ws.receive_json(timeout=2)
        assert msg["starred"] is False


async def test_cards_state_put_rejects_non_bool_starred(aiohttp_client, app_factory):
    """``starred`` must be a ``bool`` — strings / ints are rejected with 400."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    sid = (await resp.json())["session_id"]

    resp = await client.put(f"/api/cards/{sid}/state", json={"starred": "yes"})
    assert resp.status == 400

    resp = await client.put(f"/api/cards/{sid}/state", json={"starred": 1})
    assert resp.status == 400


async def test_cards_state_put_empty_patch_is_noop(aiohttp_client, app_factory):
    """Empty patch returns 200 and does not broadcast."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash")
    sid = (await resp.json())["session_id"]

    resp = await client.put(f"/api/cards/{sid}/state", json={})
    assert resp.status == 200
    result = await resp.json()
    assert result["status"] == "ok"


async def test_legacy_rename_still_broadcasts_via_generic_path(aiohttp_client, app_factory):
    """Legacy /rename URL continues to work and broadcasts card_updated identically."""
    client = await aiohttp_client(app_factory())
    async with client.ws_connect("/ws/control") as ws:
        resp = await client.post("/api/claude/terminal/create?cmd=bash")
        sid = (await resp.json())["session_id"]
        await ws.receive_json(timeout=2)  # drain card_created

        resp = await client.put(
            f"/api/claude/terminal/{sid}/rename",
            json={"display_name": "Legacy"},
        )
        assert resp.status == 200
        result = await resp.json()
        assert result["display_name"] == "Legacy"

        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "card_updated"
        assert msg["card_id"] == sid
        assert msg["display_name"] == "Legacy"


async def test_legacy_recovery_script_still_broadcasts(aiohttp_client, app_factory):
    """Legacy /recovery-script URL continues to work via the generic path."""
    client = await aiohttp_client(app_factory())
    async with client.ws_connect("/ws/control") as ws:
        resp = await client.post("/api/claude/terminal/create?cmd=bash")
        sid = (await resp.json())["session_id"]
        await ws.receive_json(timeout=2)  # drain card_created

        resp = await client.put(
            f"/api/claude/terminal/{sid}/recovery-script",
            json={"recovery_script": "echo recovered"},
        )
        assert resp.status == 200
        result = await resp.json()
        assert result["recovery_script"] == "echo recovered"

        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "card_updated"
        assert msg["card_id"] == sid
        assert msg["recovery_script"] == "echo recovered"


# ── Ephemeral session tests (#193) ────────────────────────────────────────────


async def test_create_ephemeral_session_skips_card_registry(aiohttp_client, app_factory):
    """POST ?ephemeral=true creates a PTY session but skips CardRegistry."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=echo+hi&ephemeral=true&spawner_id=sp1")
    assert resp.status == 200
    data = await resp.json()
    session_id = data["session_id"]
    assert session_id

    # Session must exist in session_manager
    mgr = client.app["session_manager"]
    assert mgr.get_session(session_id) is not None

    # Must NOT be registered in CardRegistry
    card_reg = client.app["card_registry"]
    assert card_reg.get(session_id) is None


async def test_ephemeral_timeout_too_long(aiohttp_client, app_factory):
    """POST with ephemeral=true&timeout=121 returns HTTP 400 with ephemeral_timeout_too_long."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=sleep+200&ephemeral=true&timeout=121")
    assert resp.status == 400
    data = await resp.json()
    assert data["error"] == "ephemeral_timeout_too_long"
    assert data["max_allowed"] == 120


async def test_ephemeral_timeout_too_short(aiohttp_client, app_factory):
    """POST with ephemeral=true&timeout=0 returns HTTP 400 with ephemeral_timeout_too_long."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=echo+hi&ephemeral=true&timeout=0")
    assert resp.status == 400
    data = await resp.json()
    assert data["error"] == "ephemeral_timeout_too_long"
    assert data["max_allowed"] == 120


async def test_ephemeral_timeout_expiry_destroys_session(aiohttp_client, app_factory):
    """Ephemeral session with timeout=1 is auto-destroyed after ~1 second."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=sleep+999&ephemeral=true&timeout=1")
    assert resp.status == 200
    data = await resp.json()
    session_id = data["session_id"]

    mgr = client.app["session_manager"]
    # Session exists immediately after creation
    assert mgr.get_session(session_id) is not None

    # Wait for the timeout watcher to fire (1s + 1.0s margin to reduce CI flake)
    await asyncio.sleep(2.0)

    # Session should now be gone
    assert mgr.get_session(session_id) is None


async def test_spawner_cap_enforced_at_11th(aiohttp_client, app_factory):
    """The 11th spawn with the same spawner_id returns HTTP 429 terminal_cap_reached."""
    client = await aiohttp_client(app_factory())
    live_ids = []
    for _ in range(10):
        resp = await client.post("/api/claude/terminal/create?cmd=bash&spawner_id=capX")
        assert resp.status == 200
        data = await resp.json()
        live_ids.append(data["session_id"])

    # 11th spawn should be rejected
    resp = await client.post("/api/claude/terminal/create?cmd=bash&spawner_id=capX")
    assert resp.status == 429
    data = await resp.json()
    assert data["error"] == "terminal_cap_reached"
    assert len(data["live_session_ids"]) == 10


async def test_cap_decrements_on_delete(aiohttp_client, app_factory):
    """Deleting a spawner-tracked terminal frees a slot so the 11th succeeds."""
    client = await aiohttp_client(app_factory())
    sids = []
    for _ in range(10):
        resp = await client.post("/api/claude/terminal/create?cmd=bash&spawner_id=capY")
        assert resp.status == 200
        sids.append((await resp.json())["session_id"])

    # Delete one
    resp = await client.delete(f"/api/claude/terminal/{sids[0]}")
    assert resp.status == 200

    # Now the 11th spawn should succeed
    resp = await client.post("/api/claude/terminal/create?cmd=bash&spawner_id=capY")
    assert resp.status == 200


async def test_cap_counts_open_terminal_too(aiohttp_client, app_factory):
    """The cap applies across ephemeral and non-ephemeral spawns for the same spawner."""
    client = await aiohttp_client(app_factory())
    # 5 regular + 5 ephemeral = 10 total
    for _ in range(5):
        resp = await client.post("/api/claude/terminal/create?cmd=bash&spawner_id=capZ")
        assert resp.status == 200
    for _ in range(5):
        resp = await client.post("/api/claude/terminal/create?cmd=bash&ephemeral=true&spawner_id=capZ")
        assert resp.status == 200

    # 11th (either kind) should be rejected
    resp = await client.post("/api/claude/terminal/create?cmd=bash&spawner_id=capZ")
    assert resp.status == 429
    data = await resp.json()
    assert data["error"] == "terminal_cap_reached"


async def test_user_spawned_no_spawner_id_uncapped(aiohttp_client, app_factory):
    """Spawns without spawner_id are not capped (user-facing terminals)."""
    client = await aiohttp_client(app_factory())
    # Spawn 12 terminals without spawner_id — all should succeed
    for _ in range(12):
        resp = await client.post("/api/claude/terminal/create?cmd=bash")
        assert resp.status == 200


async def test_delete_works_on_ephemeral_session(aiohttp_client, app_factory):
    """DELETE /api/claude/terminal/{id} works on ephemeral sessions (no card in registry)."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=sleep+999&ephemeral=true")
    assert resp.status == 200
    data = await resp.json()
    session_id = data["session_id"]

    # Verify session exists, no card
    mgr = client.app["session_manager"]
    card_reg = client.app["card_registry"]
    assert mgr.get_session(session_id) is not None
    assert card_reg.get(session_id) is None

    # Delete should succeed
    resp = await client.delete(f"/api/claude/terminal/{session_id}")
    assert resp.status == 200

    # Session should be gone
    assert mgr.get_session(session_id) is None


async def test_card_unregister_clears_spawner_set(aiohttp_client, app_factory):
    """Emitting card:unregistered for a canvas-claude card destroys its owned sessions."""
    client = await aiohttp_client(app_factory())
    spawner = "sp-canvas-2"

    # Spawn 2 ephemeral sessions under this spawner
    sids = []
    for _ in range(2):
        resp = await client.post(f"/api/claude/terminal/create?cmd=sleep+999&ephemeral=true&spawner_id={spawner}")
        assert resp.status == 200
        sids.append((await resp.json())["session_id"])

    mgr = client.app["session_manager"]
    canvas_claude_spawns = client.app["canvas_claude_spawns"]
    assert spawner in canvas_claude_spawns
    assert len(canvas_claude_spawns[spawner]) == 2

    # Emit card:unregistered for this spawner as a canvas-claude card
    event_bus = client.app["event_bus"]
    await event_bus.emit("card:unregistered", {"card_id": spawner, "card_type": "canvas-claude"})

    # Allow the async callback to run
    await asyncio.sleep(0.1)

    # Spawner set should be removed
    assert spawner not in canvas_claude_spawns

    # Both sessions should be destroyed
    for sid in sids:
        assert mgr.get_session(sid) is None


async def test_pty_eof_decrements_cap(aiohttp_client, app_factory):
    """PTY EOF triggers destroy_session which prunes the spawner cap set."""
    client = await aiohttp_client(app_factory())
    spawner = "sp-eof-test"
    resp = await client.post(f"/api/claude/terminal/create?cmd=sleep+999&ephemeral=true&spawner_id={spawner}")
    assert resp.status == 200
    sid = (await resp.json())["session_id"]

    mgr = client.app["session_manager"]
    canvas_claude_spawns = client.app["canvas_claude_spawns"]

    # Session is tracked in the spawner set
    assert sid in canvas_claude_spawns.get(spawner, set())

    # Simulate PTY EOF by calling destroy_session directly — this exercises
    # the on_destroy callback chain the same way _pty_read_loop's finally block does.
    mgr.destroy_session(sid)

    # Give the event loop a tick to run any async callbacks
    await asyncio.sleep(0)

    # Spawner set should be pruned
    assert sid not in canvas_claude_spawns.get(spawner, set())


async def test_timer_cancelled_on_explicit_delete(aiohttp_client, app_factory):
    """DELETE /api/claude/terminal/{id} cancels the pending ephemeral timeout task."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=sleep+999&ephemeral=true&timeout=60")
    assert resp.status == 200
    sid = (await resp.json())["session_id"]

    ephemeral_timers = client.app["ephemeral_timers"]
    assert sid in ephemeral_timers
    timer = ephemeral_timers[sid]
    assert not timer.done()

    # Explicit delete
    resp = await client.delete(f"/api/claude/terminal/{sid}")
    assert resp.status == 200

    # Timer should be cancelled and removed
    assert sid not in ephemeral_timers
    assert timer.cancelled()


async def test_timeout_without_ephemeral_rejected(aiohttp_client, app_factory):
    """POST with timeout but no ephemeral=true returns HTTP 400 timeout_requires_ephemeral."""
    client = await aiohttp_client(app_factory())
    resp = await client.post("/api/claude/terminal/create?cmd=bash&timeout=30")
    assert resp.status == 400
    data = await resp.json()
    assert data["error"] == "timeout_requires_ephemeral"


async def test_cap_decrements_on_timeout(aiohttp_client, app_factory):
    """Ephemeral timeout expiry prunes the spawner set (not just session)."""
    client = await aiohttp_client(app_factory())
    spawner = "sp-timeout-cap"
    resp = await client.post(f"/api/claude/terminal/create?cmd=sleep+999&ephemeral=true&timeout=1&spawner_id={spawner}")
    assert resp.status == 200
    sid = (await resp.json())["session_id"]

    canvas_claude_spawns = client.app["canvas_claude_spawns"]
    assert sid in canvas_claude_spawns.get(spawner, set())

    # Wait for the timeout watcher to fire (1s + 1.0s margin)
    await asyncio.sleep(2.0)

    # Spawner set should be pruned after timeout-triggered destroy
    assert sid not in canvas_claude_spawns.get(spawner, set())
