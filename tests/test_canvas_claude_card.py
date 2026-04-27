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


async def test_canvas_claude_card_no_profile_defaults_to_main(monkeypatch):
    """Without an explicit profile, the card falls back to the main slot (#163)."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")
    assert "CLAUDE_CONFIG_DIR=/profiles/main" in card.cmd


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


async def test_canvas_claude_card_dotted_profile_accepted():
    """Profile name with dots (e.g. foo.bar) must be accepted — dots are valid filesystem chars."""
    mgr = SessionManager()
    # Should not raise
    card = CanvasClaudeCard(session_manager=mgr, container="my-container", profile="foo.bar")
    assert card.profile == "foo.bar"
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
    """_seed_claude_settings writes into /profiles/main when no profile is passed (#163).

    Mirrors the __init__ fallback so the settings.json written for the trust
    prompt and the directory referenced by CLAUDE_CONFIG_DIR stay consistent.
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
    assert "/profiles/main" in shell_script
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


# ── Spawner ID injection tests (#193) ─────────────────────────────────────────


def test_build_mcp_config_includes_spawner_id():
    """_build_mcp_config appends --spawner-id <id> to the args list when provided."""
    cfg = _build_mcp_config("http://host.docker.internal:3000", spawner_id="card-abc-123")
    args = cfg["mcpServers"]["canvas"]["args"]
    assert "--spawner-id" in args
    idx = args.index("--spawner-id")
    assert args[idx + 1] == "card-abc-123"


def test_build_mcp_config_no_spawner_id_omits_flag():
    """_build_mcp_config does NOT include --spawner-id when spawner_id is None."""
    cfg = _build_mcp_config("http://host.docker.internal:3000", spawner_id=None)
    args = cfg["mcpServers"]["canvas"]["args"]
    assert "--spawner-id" not in args


def test_canvas_claude_card_mcp_args_include_spawner_id(monkeypatch):
    """CanvasClaudeCard injects --spawner-id <card.id> into MCP subprocess args."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="my-container")

    mcp_args = card._mcp_config["mcpServers"]["canvas"]["args"]
    assert "--spawner-id" in mcp_args
    idx = mcp_args.index("--spawner-id")
    # The value must match the card's own id
    assert mcp_args[idx + 1] == card.id
    mgr.stop_all()


# ── Hydration tests (epic #254 child 6, issue #261) ────────────────────────


async def test_canvas_claude_from_descriptor_builds_card(monkeypatch):
    """from_descriptor reconstructs the card with hydrated flag, no PTY started."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    data = {
        "type": "canvas_claude",
        "card_id": "cc-hydrated-1",
        "container": "my-container",
        "profile": "alice",
        "canvas_name": "my-canvas",
        "starred": True,
        "x": 10,
        "y": 20,
        "w": 800,
        "h": 600,
    }
    card = CanvasClaudeCard.from_descriptor(data, session_manager=mgr)
    assert card.session is None  # PTY not started
    assert card._hydrated is True
    assert card.profile == "alice"
    assert card.canvas_name == "my-canvas"
    assert card._effective_container == "my-container"
    assert card.starred is True
    assert card.x == 10 and card.y == 20 and card.w == 800 and card.h == 600
    mgr.stop_all()


async def test_canvas_claude_from_descriptor_requires_session_manager():
    """from_descriptor without session_manager raises TypeError (mirrors TerminalCard)."""
    with pytest.raises(TypeError):
        CanvasClaudeCard.from_descriptor({"type": "canvas_claude"})


async def test_canvas_claude_to_descriptor_emits_card_id(monkeypatch):
    """to_descriptor emits card_id (Scenario 5 falsifiability check)."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = CanvasClaudeCard(session_manager=mgr, container="c", card_id="cc-fixture-id", canvas_name="x")
    desc = card.to_descriptor()
    assert "card_id" in desc
    assert desc["card_id"] == "cc-fixture-id"
    mgr.stop_all()


async def test_canvas_claude_attach_without_existing_tmux_sets_error_state(monkeypatch):
    """attach() lands the card in error_state when tmux session is missing (Scenario 4)."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    # tmux has-session returns rc=1 (no session)
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run_no_tmux)
    mgr = SessionManager()
    card = CanvasClaudeCard.from_descriptor(
        {"type": "canvas_claude", "container": "missing-container", "card_id": "cc-2"},
        session_manager=mgr,
    )
    callbacks = []

    def _on_error(c):
        callbacks.append(c.error_state)

    await card.start(on_error_state=_on_error)
    assert card.session is None  # no PTY allocated
    assert card.error_state is not None
    assert card.error_state["kind"] == "tmux_session_missing"
    assert card.error_state["container"] == "missing-container"
    assert card.error_state["tmux_session"] == TMUX_SESSION_NAME
    assert callbacks == [card.error_state]
    mgr.stop_all()


async def test_canvas_claude_attach_with_existing_tmux_starts_pty(monkeypatch):
    """attach() with a live tmux session sets the attach cmd and allocates a PTY (Scenario 1)."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    # tmux has-session returns rc=0 (session exists)
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run)
    mgr = SessionManager()
    card = CanvasClaudeCard.from_descriptor(
        {"type": "canvas_claude", "container": "live-container", "card_id": "cc-3"},
        session_manager=mgr,
    )
    await card.start()
    assert card.error_state is None
    assert card.session is not None
    assert "tmux attach-session" in card.cmd
    assert TMUX_SESSION_NAME in card.cmd
    await card.stop()
    mgr.stop_all()


async def test_canvas_claude_attach_does_not_seed_settings(monkeypatch):
    """Hydration attach path never calls _seed_claude_settings (Scenario 1 invariant)."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run)
    seed_calls = {"n": 0}

    def counting_seed(self):
        seed_calls["n"] += 1

    monkeypatch.setattr(CanvasClaudeCard, "_seed_claude_settings", counting_seed)
    mgr = SessionManager()
    card = CanvasClaudeCard.from_descriptor(
        {"type": "canvas_claude", "container": "live-container"},
        session_manager=mgr,
    )
    await card.start()
    assert seed_calls["n"] == 0
    await card.stop()
    mgr.stop_all()


async def test_canvas_claude_attach_does_not_create_new_tmux_session(monkeypatch):
    """Hydration must never call ``tmux new-session`` even when the session is missing.

    Falsifiability check for Scenario 1's invariant: a hydrated card with a
    missing tmux session lands in error_state — it must NOT silently create a
    fresh session.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    calls: list[list[str]] = []

    def track_run(cmd_list, **kw):
        calls.append(list(cmd_list))
        # has-session → rc=1 (no session); any other tmux call would be an
        # invariant violation.
        return type("R", (), {"returncode": 1, "stderr": b""})()

    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", track_run)
    mgr = SessionManager()
    card = CanvasClaudeCard.from_descriptor(
        {"type": "canvas_claude", "container": "ghost"},
        session_manager=mgr,
    )
    await card.start()
    new_session_calls = [c for c in calls if "new-session" in c]
    assert new_session_calls == []
    mgr.stop_all()


def test_preset_canvas_claude_entries_have_card_id():
    """Every canvas_claude entry in dev_presets/*/canvases/*.json carries a stable card_id."""
    import json as _json
    import pathlib as _pathlib

    preset_root = _pathlib.Path(__file__).resolve().parents[1] / "claude_rts" / "dev_presets"
    missing: list[str] = []
    for canvas_file in preset_root.glob("*/canvases/*.json"):
        data = _json.loads(canvas_file.read_text())
        for idx, card in enumerate(data.get("cards", [])):
            if card.get("type") == "canvas_claude" and not card.get("card_id"):
                missing.append(f"{canvas_file}: idx={idx}")
    assert not missing, "canvas_claude entries without card_id:\n" + "\n".join(missing)


async def test_hydrate_canvas_claude_into_registry(tmp_path, monkeypatch):
    """hydrate_canvas_into_registry registers a canvas_claude entry into CardRegistry.

    Covers Scenario 1: a canvas JSON containing a canvas_claude entry causes a
    CanvasClaudeCard to exist in CardRegistry after hydration runs, attaching
    to the existing tmux session without creating a new one.
    """
    import json as _json

    from claude_rts import config as _config
    from claude_rts.cards.card_registry import CardRegistry as _CR
    from claude_rts.server import create_app as _create_app

    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    # tmux has-session → rc=0 (alive). All other subprocess calls also succeed.
    monkeypatch.setattr("claude_rts.cards.canvas_claude_card._subprocess.run", _mock_subprocess_run)

    app_config = _config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "name": "cc-canvas",
        "canvas_size": [3840, 2160],
        "cards": [
            {
                "type": "canvas_claude",
                "card_id": "cc-hydrated",
                "container": "supreme-claudemander-util",
                "profile": "main",
                "canvas_name": "cc-canvas",
                "starred": True,
                "x": 100,
                "y": 100,
                "w": 960,
                "h": 640,
            }
        ],
    }
    (app_config.canvases_dir / "cc-canvas.json").write_text(_json.dumps(snapshot))

    app = _create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]

    import asyncio as _asyncio

    for hook in app.on_startup:
        await hook(app)
    try:
        await _asyncio.sleep(0.1)  # let background start() task run attach()
        registry: _CR = app["card_registry"]
        all_cards = registry.cards_on_canvas("cc-canvas")
        cc_cards = [c for c in all_cards if isinstance(c, CanvasClaudeCard)]
        assert len(cc_cards) == 1
        cc = cc_cards[0]
        assert cc._hydrated is True
        assert cc.error_state is None
        assert cc.session is not None  # PTY allocated by attach()
        assert "tmux attach-session" in cc.cmd
    finally:
        for hook in app.on_shutdown:
            try:
                await hook(app)
            except Exception:
                pass
