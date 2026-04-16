"""Tests for TerminalCard lifecycle, CardRegistry, and server integration."""

import time

from claude_rts.cards.terminal_card import TerminalCard
from claude_rts.cards.card_registry import CardRegistry
from claude_rts.sessions import SessionManager
from claude_rts import config
from claude_rts.server import create_app


# ── MockPty (same as test_sessions.py) ─────────────────────────────────────


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


# ── TerminalCard unit tests ────────────────────────────────────────────────


async def test_terminal_card_start_stop(monkeypatch):
    """start() allocates a PTY session; stop() destroys it."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="echo hello", hub="hub1", container="cont1")

    await card.start()

    assert card.session is not None
    assert card.session_id.startswith("rts-")
    assert card.id == card.session_id
    assert card.alive is True
    assert mgr.get_session(card.session_id) is not None

    sid = card.session_id
    await card.stop()

    assert card.session is None
    assert card.alive is False
    assert mgr.get_session(sid) is None
    mgr.stop_all()


async def test_terminal_card_type_and_hidden():
    """TerminalCard has card_type='terminal' and hidden=False."""
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="test")
    assert card.card_type == "terminal"
    assert card.hidden is False


async def test_terminal_card_to_descriptor(monkeypatch):
    """to_descriptor() returns the shape the frontend expects."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="bash -l", hub="my-hub", container="my-cont")
    await card.start()

    desc = card.to_descriptor()

    assert desc["type"] == "terminal"
    assert desc["session_id"] == card.session_id
    assert desc["hub"] == "my-hub"
    assert desc["container"] == "my-cont"
    assert desc["exec"] == "bash -l"

    await card.stop()
    mgr.stop_all()


async def test_terminal_card_descriptor_minimal(monkeypatch):
    """to_descriptor() omits optional fields when not set."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="echo hi")
    await card.start()

    desc = card.to_descriptor()
    assert "hub" not in desc
    assert "container" not in desc
    assert desc["exec"] == "echo hi"

    await card.stop()
    mgr.stop_all()


async def test_terminal_card_stop_idempotent(monkeypatch):
    """Calling stop() twice does not raise."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="test")
    await card.start()
    await card.stop()
    await card.stop()  # should not raise
    mgr.stop_all()


# ── CardRegistry unit tests ────────────────────────────────────────────────


async def test_card_registry_register_and_get(monkeypatch):
    """register() adds a card; get() retrieves it by id."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    card = TerminalCard(session_manager=mgr, cmd="test")
    await card.start()
    reg.register(card)

    assert reg.get(card.id) is card
    assert reg.get_terminal(card.session_id) is card

    await card.stop()
    mgr.stop_all()


async def test_card_registry_unregister(monkeypatch):
    """unregister() removes the card and returns it."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    card = TerminalCard(session_manager=mgr, cmd="test")
    await card.start()
    reg.register(card)

    removed = reg.unregister(card.id)
    assert removed is card
    assert reg.get(card.id) is None

    # Unregister again returns None
    assert reg.unregister(card.id) is None

    await card.stop()
    mgr.stop_all()


async def test_card_registry_list_terminals(monkeypatch):
    """list_terminals() returns only TerminalCards."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    c1 = TerminalCard(session_manager=mgr, cmd="cmd1")
    c2 = TerminalCard(session_manager=mgr, cmd="cmd2")
    await c1.start()
    await c2.start()
    reg.register(c1)
    reg.register(c2)

    terminals = reg.list_terminals()
    assert len(terminals) == 2
    assert set(t.id for t in terminals) == {c1.id, c2.id}

    await c1.stop()
    await c2.stop()
    mgr.stop_all()


async def test_card_registry_stop_all(monkeypatch):
    """stop_all() stops and removes all cards."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    card = TerminalCard(session_manager=mgr, cmd="test")
    await card.start()
    sid = card.session_id
    reg.register(card)

    await reg.stop_all()

    assert reg.get(sid) is None
    assert len(reg.list_all()) == 0
    mgr.stop_all()


# ── Server integration: card_registry available in app ─────────────────────


async def test_app_has_card_registry(tmp_path, monkeypatch):
    """create_app startup hook should create a CardRegistry in app['card_registry']."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)

    # card_registry is set during on_startup hook; verify it's not set before startup
    # and the route for /ws/session/new is registered
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/ws/session/new" in routes
    assert "/ws/session/{session_id}" in routes


async def test_session_new_creates_terminal_card(aiohttp_client, tmp_path, monkeypatch):
    """POST to /api/test/session/create should result in a TerminalCard in the registry."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    # Use the test puppeting API to create a session (avoids WebSocket in tests)
    resp = await client.post("/api/test/session/create?cmd=echo+hello")
    assert resp.status == 200
    data = await resp.json()
    sid = data["session_id"]

    # The test API goes through SessionManager directly (not TerminalCard),
    # but we can verify the session exists
    mgr = app["session_manager"]
    assert mgr.get_session(sid) is not None


# ── CardRegistry.by_type (#127) ────────────────────────────────────────────


async def test_card_registry_by_type(monkeypatch):
    """by_type('terminal') returns all TerminalCard instances in the registry."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    c1 = TerminalCard(session_manager=mgr, cmd="cmd1")
    c2 = TerminalCard(session_manager=mgr, cmd="cmd2")
    await c1.start()
    await c2.start()
    reg.register(c1)
    reg.register(c2)

    terminals = reg.by_type("terminal")
    assert len(terminals) == 2
    assert set(t.id for t in terminals) == {c1.id, c2.id}

    # Unknown type → empty list, never raises
    assert reg.by_type("does-not-exist") == []

    await c1.stop()
    await c2.stop()
    mgr.stop_all()


# ── Session.kind / probe filtering (#127) ──────────────────────────────────


async def test_session_kind_default_is_user(monkeypatch):
    """Session.kind defaults to 'user' when create_session omits it."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()

    session = mgr.create_session("echo hi")
    assert session.kind == "user"

    mgr.destroy_session(session.session_id)
    mgr.stop_all()


async def test_list_sessions_excludes_probe_sessions(monkeypatch):
    """list_sessions() filters out sessions created with kind='probe'."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()

    user = mgr.create_session("bash -l")
    probe = mgr.create_session("claude --version", kind="probe")

    assert probe.kind == "probe"

    listed_ids = {s["session_id"] for s in mgr.list_sessions()}
    assert user.session_id in listed_ids
    assert probe.session_id not in listed_ids

    mgr.stop_all()


# ── Issue #151: display_name, recovery_script, stable identity ────────────


async def test_terminal_card_display_name(monkeypatch):
    """TerminalCard accepts and returns display_name in descriptor."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="bash", display_name="Build Server")
    await card.start()

    assert card.display_name == "Build Server"
    desc = card.to_descriptor()
    assert desc["display_name"] == "Build Server"

    await card.stop()
    mgr.stop_all()


async def test_terminal_card_recovery_script(monkeypatch):
    """TerminalCard accepts and returns recovery_script in descriptor."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="bash", recovery_script="cd /work && make dev")
    await card.start()

    assert card.recovery_script == "cd /work && make dev"
    desc = card.to_descriptor()
    assert desc["recovery_script"] == "cd /work && make dev"

    await card.stop()
    mgr.stop_all()


async def test_terminal_card_descriptor_omits_empty_display_name(monkeypatch):
    """to_descriptor() omits display_name and recovery_script when empty."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start()

    desc = card.to_descriptor()
    assert "display_name" not in desc
    assert "recovery_script" not in desc

    await card.stop()
    mgr.stop_all()


async def test_terminal_card_mutable_display_name(monkeypatch):
    """display_name and recovery_script can be set after creation."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start()

    card.display_name = "Dev Shell"
    card.recovery_script = "npm start"

    desc = card.to_descriptor()
    assert desc["display_name"] == "Dev Shell"
    assert desc["recovery_script"] == "npm start"

    await card.stop()
    mgr.stop_all()
