"""Tests for CanvasClaudeCard lifecycle and descriptor."""

import base64
import json
import time

import pytest

from claude_rts.cards.canvas_claude_card import (
    CONTAINER_MCP_SERVER,
    CONTAINER_PYTHON3,
    CanvasClaudeCard,
    TMUX_SESSION_NAME,
    _DOCKER,
    _build_mcp_config,
)
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
    assert "my-container" in card.cmd
    # MCP config is written into settings.json via _seed_claude_settings(),
    # not passed as a --mcp-config flag on the command line.


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


# ── MCP config and trust-seeding tests (issue #114) ────────────────────────


def test_build_mcp_config_uses_absolute_python3():
    """_build_mcp_config uses absolute python3 path so env stripping can't break spawn."""
    cfg = _build_mcp_config("http://host.docker.internal:3000")
    server = cfg["mcpServers"]["canvas"]
    assert server["command"] == CONTAINER_PYTHON3
    assert server["command"] == "/usr/local/bin/python3"


def test_build_mcp_config_passes_api_base_via_argv():
    """_build_mcp_config passes --api-base <url> in args (not only in env)."""
    cfg = _build_mcp_config("http://example.test:4000")
    args = cfg["mcpServers"]["canvas"]["args"]
    assert CONTAINER_MCP_SERVER in args
    assert "--api-base" in args
    idx = args.index("--api-base")
    assert args[idx + 1] == "http://example.test:4000"


def test_build_mcp_config_env_contains_inherited_keys():
    """_build_mcp_config env re-declares PATH/HOME/USER/LANG plus API env var.

    This survives the failure mode where Claude Code replaces (rather than
    merges) the parent environment when spawning the MCP subprocess.
    """
    cfg = _build_mcp_config("http://host.docker.internal:3000")
    env = cfg["mcpServers"]["canvas"]["env"]
    for key in ("PATH", "HOME", "USER", "LANG", "SUPREME_CLAUDEMANDER_API"):
        assert key in env, f"env missing {key}: {env}"
    assert "/usr/local/bin" in env["PATH"]
    assert env["SUPREME_CLAUDEMANDER_API"] == "http://host.docker.internal:3000"


def test_build_mcp_config_round_trip_through_base64():
    """The base64-encoded config decodes back to the same dict."""
    cfg = _build_mcp_config("http://host.docker.internal:3000")
    encoded = base64.b64encode(json.dumps(cfg).encode()).decode()
    decoded = json.loads(base64.b64decode(encoded).decode())
    assert decoded == cfg


async def test_canvas_claude_card_stores_mcp_sha256(monkeypatch):
    """Card exposes _mcp_sha256 for post-mortem diagnostics."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    assert isinstance(card._mcp_sha256, str)
    assert len(card._mcp_sha256) == 64  # hex digest length
    mgr.stop_all()


async def test_canvas_claude_card_seed_trust_settings_profile(monkeypatch):
    """_seed_claude_settings writes into /profiles/<profile> when profile is set."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    calls = []

    def track_run(cmd_list, **kw):
        calls.append(cmd_list)
        return type("R", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", track_run)
    mgr = SessionManager()
    card = CanvasClaudeCard(
        session_manager=mgr,
        container="my-container",
        profile="alice",
    )
    card._seed_claude_settings()
    # _seed_claude_settings makes exactly 2 subprocess.run calls when both succeed:
    # call[0] writes trust settings JSON; call[1] runs the MCP .claude.json patch via python3.
    assert len(calls) == 2
    # First call: trust settings write
    cmd = calls[0]
    assert cmd[0] == _DOCKER
    assert cmd[1] == "exec"
    assert "my-container" in cmd
    shell_script = cmd[-1]
    assert "/profiles/alice" in shell_script
    assert "settings.json" in shell_script
    assert "base64 -d" in shell_script
    # Second call: MCP .claude.json patch via python3
    mcp_cmd = calls[1]
    assert mcp_cmd[0] == _DOCKER
    assert mcp_cmd[1] == "exec"
    assert "my-container" in mcp_cmd
    assert "python3" in mcp_cmd[-1]
    mgr.stop_all()


async def test_canvas_claude_card_seed_trust_settings_no_profile(monkeypatch):
    """_seed_claude_settings falls back to /home/util/.claude when no profile.

    The util container runs as user ``util`` with home ``/home/util`` (see
    Dockerfile.util), so that's the correct default config dir for claude.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    calls = []

    def track_run(cmd_list, **kw):
        calls.append(cmd_list)
        return type("R", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", track_run)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    card._seed_claude_settings()
    shell_script = calls[0][-1]
    assert "/home/util/.claude" in shell_script
    mgr.stop_all()


async def test_canvas_claude_card_seed_trust_settings_payload_decodes(monkeypatch):
    """The base64 payload in the seed command decodes to the expected JSON."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    captured = {}

    def track_run(cmd_list, **kw):
        # Keep only the FIRST call (trust settings write); the second call is the MCP patch.
        captured.setdefault("cmd", cmd_list)
        return type("R", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", track_run)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    card._seed_claude_settings()
    shell_script = captured["cmd"][-1]
    # Extract the base64 blob (appears between "echo " and " | base64 -d")
    b64_blob = shell_script.split("echo ", 1)[1].split(" |", 1)[0]
    decoded = json.loads(base64.b64decode(b64_blob).decode())
    assert decoded.get("hasTrustDialogAccepted") is True
    mgr.stop_all()


async def test_canvas_claude_card_start_seeds_trust_on_new_session(monkeypatch):
    """start() calls _seed_claude_settings on the new-session path."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)

    def run_no_tmux(cmd_list, **kw):
        # tmux has-session → rc=1 (no session) so _ensure_tmux_session picks new
        if "has-session" in cmd_list:
            return type("R", (), {"returncode": 1, "stderr": b""})()
        return type("R", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", run_no_tmux)

    seed_calls = {"n": 0}
    orig_seed = CanvasClaudeCard._seed_claude_settings

    def counting_seed(self):
        seed_calls["n"] += 1
        return orig_seed(self)

    monkeypatch.setattr(CanvasClaudeCard, "_seed_claude_settings", counting_seed)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    await card.start()
    assert seed_calls["n"] == 1
    assert "tmux new-session" in card.cmd
    await card.stop()
    mgr.stop_all()


async def test_canvas_claude_card_start_skips_seed_on_attach(monkeypatch):
    """start() does NOT call _seed_claude_settings on the attach path."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    # _mock_subprocess_run returns rc=0 → tmux has-session succeeds → attach
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run)

    seed_calls = {"n": 0}

    def counting_seed(self):
        seed_calls["n"] += 1

    monkeypatch.setattr(CanvasClaudeCard, "_seed_claude_settings", counting_seed)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    await card.start()
    assert seed_calls["n"] == 0
    assert "tmux attach-session" in card.cmd
    await card.stop()
    mgr.stop_all()


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
