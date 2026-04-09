"""Tests for CanvasClaudeCard lifecycle and descriptor."""

import time

import pytest

from claude_rts.cards.canvas_claude_card import CanvasClaudeCard, TMUX_SESSION_NAME
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


def _mock_subprocess_run(*a, **kw):
    return type("R", (), {"returncode": 0, "stderr": b""})()


def _mock_subprocess_run_no_tmux(*a, **kw):
    """Mock that returns failure for tmux has-session (no existing session)."""
    return type("R", (), {"returncode": 1, "stderr": b"no session"})()


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
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run)
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
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run)
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
    assert desc.get("container") == "my-container"
    await card.stop()
    mgr.stop_all()


async def test_canvas_claude_card_new_session(monkeypatch):
    """new_session() stops the old PTY and starts a fresh one."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run)
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
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="test-container")
    await card.start()
    await card.clear_session()
    written = card.session.pty._written
    assert any("/clear" in w for w in written)
    await card.stop()
    mgr.stop_all()


async def test_canvas_claude_card_cmd_includes_tmux(monkeypatch):
    """The computed cmd includes tmux new-session and the claude command."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    assert "tmux new-session" in card.cmd
    assert TMUX_SESSION_NAME in card.cmd
    assert "mcp.json" in card.cmd
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


async def test_canvas_claude_card_no_bash_wrapper(monkeypatch):
    """The PTY cmd does NOT use a bash -c wrapper (direct docker exec pattern)."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    assert "bash -c" not in card.cmd


async def test_canvas_claude_card_bypass_permissions(monkeypatch):
    """The PTY cmd always includes --dangerously-skip-permissions."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    assert "--dangerously-skip-permissions" in card.cmd


async def test_canvas_claude_card_default_api_url(monkeypatch):
    """Default api_base_url is host.docker.internal:3000."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    assert card.api_base_url == "http://host.docker.internal:3000"


async def test_canvas_claude_card_invalid_container_rejected():
    """Container name with shell-unsafe characters raises ValueError."""
    mgr = SessionManager()
    with pytest.raises(ValueError):
        CanvasClaudeCard(session_manager=mgr, container="foo'; rm -rf /")
    mgr.stop_all()


async def test_canvas_claude_card_invalid_profile_rejected():
    """Profile name with shell-unsafe characters raises ValueError."""
    mgr = SessionManager()
    with pytest.raises(ValueError):
        CanvasClaudeCard(session_manager=mgr, container="my-container", profile="x; evil")
    mgr.stop_all()


async def test_canvas_claude_card_ensure_tmux_attach(monkeypatch):
    """_ensure_tmux_session sets cmd to attach when tmux session exists."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    # _mock_subprocess_run returns rc=0 → tmux session exists
    card._ensure_tmux_session()
    assert "tmux attach-session" in card.cmd
    assert TMUX_SESSION_NAME in card.cmd
    assert "--mcp-config" not in card.cmd  # attach doesn't include the full claude cmd


async def test_canvas_claude_card_ensure_tmux_new(monkeypatch):
    """_ensure_tmux_session sets cmd to new-session when no tmux session exists."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run_no_tmux)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    card._ensure_tmux_session()
    assert "tmux new-session" in card.cmd
    assert TMUX_SESSION_NAME in card.cmd
    assert "claude" in card.cmd


async def test_canvas_claude_card_new_session_kills_tmux(monkeypatch):
    """new_session() kills the tmux session before restarting."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    calls = []

    def track_subprocess_run(cmd_list, **kw):
        calls.append(cmd_list)
        return type("R", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", track_subprocess_run)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="test-container")
    await card.start()
    calls.clear()
    await card.new_session()
    # Should have called kill-session at some point
    kill_calls = [c for c in calls if "kill-session" in c]
    assert len(kill_calls) >= 1
    await card.stop()
    mgr.stop_all()
