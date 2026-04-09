"""Tests for CanvasClaudeCard lifecycle and descriptor."""

import time

from claude_rts.cards.canvas_claude_card import CanvasClaudeCard
from claude_rts.sessions import SessionManager


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


async def test_canvas_claude_card_type():
    """card_type is 'canvas_claude' and hidden is False."""
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="test-container")
    assert card.card_type == "canvas_claude"
    assert card.hidden is False
    mgr.stop_all()


async def test_canvas_claude_card_start_stop(monkeypatch):
    """start() allocates a PTY session; stop() destroys it."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="test-container")
    await card.start()
    assert card.alive is True
    assert card.session_id is not None
    sid = card.session_id
    await card.stop()
    assert card.alive is False
    assert mgr.get_session(sid) is None
    mgr.stop_all()


async def test_canvas_claude_card_to_descriptor(monkeypatch):
    """to_descriptor() returns type='canvas_claude' with profile and canvas_name."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(
        session_manager=mgr,
        container="my-container",
        profile="test-profile",
        canvas_name="my-canvas",
    )
    await card.start()
    desc = card.to_descriptor()
    assert desc["type"] == "canvas_claude"
    assert desc["session_id"] == card.session_id
    assert desc.get("profile") == "test-profile"
    assert desc.get("canvas_name") == "my-canvas"
    await card.stop()
    mgr.stop_all()


async def test_canvas_claude_card_new_session(monkeypatch):
    """new_session() stops the old PTY and starts a fresh one."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="test-container")
    await card.start()
    sid1 = card.session_id
    await card.new_session()
    assert card.alive is True
    assert card.session_id != sid1  # new session ID
    await card.stop()
    mgr.stop_all()


async def test_canvas_claude_card_clear_session(monkeypatch):
    """clear_session() writes /clear to the PTY."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="test-container")
    await card.start()
    await card.clear_session()
    written = card.session.pty._written
    assert any("/clear" in w for w in written)
    await card.stop()
    mgr.stop_all()


async def test_canvas_claude_card_cmd_includes_mcp(monkeypatch):
    """The computed cmd includes MCP config and docker exec."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    assert "mcp_server.py" in card.cmd or "mcp.json" in card.cmd
    assert "my-container" in card.cmd


async def test_canvas_claude_card_cmd_includes_profile(monkeypatch):
    """The computed cmd includes profile in CLAUDE_CONFIG_DIR when profile is set."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(
        session_manager=mgr,
        container="my-container",
        profile="test-profile",
    )
    assert "test-profile" in card.cmd


async def test_canvas_claude_card_no_profile(monkeypatch):
    """The computed cmd works without profile (no CLAUDE_CONFIG_DIR)."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    assert "CLAUDE_CONFIG_DIR" not in card.cmd


async def test_canvas_claude_card_default_api_url(monkeypatch):
    """Default api_base_url is host.docker.internal:3000."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    assert card.api_base_url == "http://host.docker.internal:3000"


async def test_canvas_claude_card_invalid_container_rejected():
    """Container name with shell-unsafe characters raises ValueError."""
    mgr = SessionManager()
    try:
        CanvasClaudeCard(session_manager=mgr, container="foo'; rm -rf /")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    mgr.stop_all()


async def test_canvas_claude_card_invalid_profile_rejected():
    """Profile name with shell-unsafe characters raises ValueError."""
    mgr = SessionManager()
    try:
        CanvasClaudeCard(session_manager=mgr, container="my-container", profile="x; evil")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    mgr.stop_all()
