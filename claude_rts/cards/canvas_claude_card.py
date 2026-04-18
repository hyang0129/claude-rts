"""CanvasClaudeCard: runs `claude` CLI with the canvas MCP server inside the util container."""

import asyncio as _asyncio
import base64 as _b64
import hashlib as _hashlib
import json as _json
import pathlib as _pathlib
import re as _re
import subprocess as _subprocess
from loguru import logger

from .terminal_card import TerminalCard

_DOCKER = "docker"

# Only alphanumeric, hyphens, and underscores are safe for shell interpolation.
_SAFE_NAME = _re.compile(r"^[a-zA-Z0-9_-]+$")

# Stable tmux session name used for canvas claude persistence.
# Only one canvas claude per container is supported, so a fixed name is fine.
TMUX_SESSION_NAME = "canvas-claude"

# Absolute path to python3 inside the util container (python:3.11-slim image).
# Hardcoded because Claude Code may pass ``env`` to the MCP subprocess without
# inheriting PATH — a bare ``python3`` would silently fail to spawn.
CONTAINER_PYTHON3 = "/usr/local/bin/python3"

# Path to the synced MCP server script inside the util container.
CONTAINER_MCP_SERVER = "/home/util/mcp_server.py"


def _build_mcp_config(api_base_url: str) -> dict:
    """Build the Claude Code MCP server config for the canvas tools.

    Belt-and-suspenders strategy to survive any ``env`` semantics quirk in
    Claude Code's MCP client:

    1. **Absolute interpreter path** — ``command`` is the absolute path to
       python3 in the util container, so the child process starts even if the
       inherited PATH is empty.
    2. **URL via argv, not env** — ``--api-base <url>`` is passed as an ``args``
       element so ``mcp_server.py`` resolves the API base without depending on
       ``SUPREME_CLAUDEMANDER_API`` at all.
    3. **Explicit env merge** — PATH / HOME / USER / LANG are re-declared in
       ``env`` so that, even if Claude Code replaces (rather than merges with)
       the parent environment, the subprocess still has everything it needs.
       ``SUPREME_CLAUDEMANDER_API`` is also kept for backward compat with older
       MCP servers on disk.
    """
    return {
        "mcpServers": {
            "canvas": {
                "command": CONTAINER_PYTHON3,
                "args": [
                    CONTAINER_MCP_SERVER,
                    "--api-base",
                    api_base_url,
                ],
                "env": {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": "/home/util",
                    "USER": "util",
                    "LANG": "C.UTF-8",
                    "SUPREME_CLAUDEMANDER_API": api_base_url,
                },
            }
        }
    }


# JSON written into the container's claude settings file to pre-trust the
# workspace. Claude Code 2.x checks ``hasTrustDialogAccepted`` before showing
# the "Yes, I trust this folder" TUI prompt; pre-seeding it avoids a race
# between the trust prompt and MCP handshake.
_TRUST_SETTINGS = {
    "hasTrustDialogAccepted": True,
    "hasCompletedOnboarding": True,
    # Suppress the "WARNING: Claude Code running in Bypass Permissions mode" dialog
    # that appears when --dangerously-skip-permissions is passed. Without this,
    # an interactive TUI prompt blocks MCP handshake on every fresh session.
    "skipDangerousModePermissionPrompt": True,
}


def _validate_name(value: str, label: str) -> None:
    """Raise ValueError if value contains characters unsafe for shell interpolation."""
    if not _SAFE_NAME.match(value):
        raise ValueError(f"Invalid {label} {value!r}: only alphanumeric, hyphens, and underscores are allowed")


class CanvasClaudeCard(TerminalCard):
    """Card that launches the Claude Code CLI with an MCP server exposing canvas tools.

    The MCP server (mcp_server.py) is written into the util container at
    /home/util/mcp_server.py and speaks the MCP stdio protocol over the
    subprocess's stdin/stdout, giving claude access to canvas control tools.

    Persistence strategy:
    The claude process runs inside a tmux session named ``canvas-claude``
    within the container.  When the Windows-side PTY (ConPTY) is closed,
    tmux keeps the process alive.  On reconnect, the PTY command is
    ``tmux attach-session -t canvas-claude`` which rebinds to the live
    session instead of spawning a fresh claude.

    Start sequence:
    1. write_mcp_config() — writes /tmp/mcp.json into the container via `docker exec`
       (non-interactive subprocess, no PTY, avoids shell-quoting issues on Windows).
    2. _ensure_tmux_session() — checks if a tmux session already exists:
       a. If yes: the PTY command is set to ``docker exec -it <container> tmux
          attach-session -t canvas-claude`` so claude resumes where it left off.
       b. If no: the PTY command creates a new tmux session running claude:
          ``docker exec -it <container> tmux new-session -s canvas-claude <claude_cmd>``
    """

    card_type: str = "canvas_claude"
    hidden: bool = False

    def __init__(
        self,
        session_manager,
        hub: str | None = None,
        container: str | None = None,
        card_id: str | None = None,
        layout: dict | None = None,
        api_base_url: str = "http://host.docker.internal:3000",
        profile: str | None = None,
        canvas_name: str | None = None,
    ):
        # Validate names that are interpolated into the shell command.
        effective_container = container or "supreme-claudemander-util"
        _validate_name(effective_container, "container")
        if profile:
            _validate_name(profile, "profile")

        # Build the MCP config JSON. Written into settings.json by
        # _seed_claude_settings() so Claude loads it automatically on start.
        mcp_config = _build_mcp_config(api_base_url)
        mcp_json = _json.dumps(mcp_config)
        self._mcp_config = mcp_config
        self._mcp_json = mcp_json
        self._mcp_sha256 = _hashlib.sha256(mcp_json.encode()).hexdigest()

        # Build the inner claude command (without docker/tmux wrapping).
        # The actual PTY command is determined at start() time by
        # _ensure_tmux_session(), which decides between attach vs new-session.
        #
        # The MCP server is registered via `claude mcp add` by _seed_claude_settings()
        # so no --mcp-config flag is needed here.
        docker_bin = _DOCKER
        if profile:
            claude_cmd = f"env CLAUDE_CONFIG_DIR=/profiles/{profile} claude --dangerously-skip-permissions"
        else:
            claude_cmd = "claude --dangerously-skip-permissions"

        # Default cmd is the new-session variant; start() may override with attach.
        cmd = f"{docker_bin} exec -it {effective_container} tmux new-session -s {TMUX_SESSION_NAME} {claude_cmd}"

        # Pass container=None so SessionManager doesn't override cmd with its
        # own tmux logic. We handle tmux ourselves with a stable session name.
        super().__init__(
            session_manager,
            cmd=cmd,
            hub=hub,
            container=None,
            card_id=card_id,
            layout=layout,
        )

        self._effective_container = effective_container
        self._claude_cmd = claude_cmd
        self.api_base_url = api_base_url
        self.profile = profile
        self.canvas_name = canvas_name

    # ── Descriptor serialization ───────────────────────────────────────

    def to_descriptor(self) -> dict:
        """Return descriptor with canvas_claude type and extra fields."""
        desc = super().to_descriptor()
        desc["type"] = "canvas_claude"
        desc["container"] = self._effective_container
        desc["profile"] = self.profile
        desc["canvas_name"] = self.canvas_name
        return desc

    # ── Container helpers ──────────────────────────────────────────────

    def _sync_mcp_server(self) -> None:
        """Copy the current mcp_server.py from the host into the container.

        Always runs at card start so the container has the latest tool definitions
        without requiring an image rebuild. Runs synchronously; call from an executor.
        """
        src = _pathlib.Path(__file__).parent.parent / "mcp_server.py"
        result = _subprocess.run(
            [_DOCKER, "cp", str(src), f"{self._effective_container}:/home/util/mcp_server.py"],
            timeout=10,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to sync mcp_server.py: {result.stderr.decode(errors='replace')}")
        logger.debug("CanvasClaudeCard: synced mcp_server.py to {}", self._effective_container)

    def _seed_claude_settings(self) -> None:
        """Pre-seed trust flags and the canvas MCP server inside the container.

        Two operations, both idempotent:

        1. **Trust settings** — writes ``settings.json`` with trust flags so
           the workspace-trust TUI dialog never fires.  Two files are written:
           - ``<target_dir>/settings.json`` (profile-level, matches
             ``CLAUDE_CONFIG_DIR`` used in ``_claude_cmd``)
           - ``/home/util/.claude/settings.json`` (fallback workspace-level)

        2. **MCP registration** — runs ``claude mcp add`` (remove-then-add so
           the entry is always current) to register the canvas MCP server in
           ``<target_dir>/.claude.json`` under ``projects["/home/util"]``.
           Claude Code reads MCP servers from ``.claude.json``, not from
           ``settings.json``, so this is the only reliable registration path.
           The ``--scope local`` default writes to the project key that matches
           the CWD (``/home/util``) inside the container.

        Runs synchronously; call from an executor when inside async context.
        """
        if self.profile:
            target_dir = f"/profiles/{self.profile}"
        else:
            target_dir = "/home/util/.claude"
        target_path = f"{target_dir}/settings.json"

        # ── 1. Trust settings ──────────────────────────────────────────────
        settings_json = _json.dumps(_TRUST_SETTINGS)
        settings_b64 = _b64.b64encode(settings_json.encode()).decode()

        workspace_dir = "/home/util/.claude"
        workspace_path = f"{workspace_dir}/settings.json"

        write_cmd = [
            _DOCKER,
            "exec",
            self._effective_container,
            "bash",
            "-c",
            (
                f"mkdir -p {target_dir} && echo {settings_b64} | base64 -d > {target_path} && "
                f"mkdir -p {workspace_dir} && echo {settings_b64} | base64 -d > {workspace_path}"
            ),
        ]
        result = _subprocess.run(write_cmd, timeout=10, capture_output=True)
        if result.returncode != 0:
            logger.warning(
                "CanvasClaudeCard: failed to seed trust settings at {}: {}",
                target_path,
                result.stderr.decode(errors="replace"),
            )
            return
        logger.debug("CanvasClaudeCard: seeded trust settings at {} and {}", target_path, workspace_path)

        # ── 2. MCP registration via .claude.json patch ────────────────────
        # Claude Code reads MCP servers from .claude.json under
        # projects[cwd]["mcpServers"], not from settings.json.
        # We patch that file directly (same result as `claude mcp add`) to avoid
        # the variadic --env flag parsing quirk in the claude CLI.
        # Format verified by running `claude mcp add` and inspecting the output.
        mcp_cfg = self._mcp_config["mcpServers"]["canvas"]
        mcp_entry = {
            "type": "stdio",
            "command": mcp_cfg["command"],
            "args": mcp_cfg["args"],
            "env": mcp_cfg.get("env", {}),
        }
        mcp_entry_json = _json.dumps(mcp_entry)
        mcp_entry_b64 = _b64.b64encode(mcp_entry_json.encode()).decode()

        claude_json_path = f"{target_dir}/.claude.json"
        mcp_patch_script = (
            f"import json, os, base64\n"
            f"p = {claude_json_path!r}\n"
            f"d = json.load(open(p)) if os.path.exists(p) else {{}}\n"
            f"entry = json.loads(base64.b64decode({mcp_entry_b64!r}))\n"
            f"d.setdefault('projects', {{}}).setdefault('/home/util', {{}})['hasTrustDialogAccepted'] = True\n"
            f"d['projects']['/home/util'].setdefault('mcpServers', {{}})['canvas'] = entry\n"
            f"json.dump(d, open(p, 'w'))\n"
        )
        mcp_patch_b64 = _b64.b64encode(mcp_patch_script.encode()).decode()

        mcp_cmd = [
            _DOCKER,
            "exec",
            self._effective_container,
            "bash",
            "-c",
            f"echo {mcp_patch_b64} | base64 -d | python3",
        ]
        result = _subprocess.run(mcp_cmd, timeout=10, capture_output=True)
        if result.returncode != 0:
            logger.warning(
                "CanvasClaudeCard: failed to register canvas MCP server in .claude.json: {}",
                result.stderr.decode(errors="replace"),
            )
            return
        logger.debug("CanvasClaudeCard: registered canvas MCP server in {}", claude_json_path)

    def _has_tmux_session(self) -> bool:
        """Check if the stable tmux session already exists in the container."""
        try:
            result = _subprocess.run(
                [
                    _DOCKER,
                    "exec",
                    self._effective_container,
                    "tmux",
                    "has-session",
                    "-t",
                    TMUX_SESSION_NAME,
                ],
                timeout=5,
                capture_output=True,
            )
            return result.returncode == 0
        except Exception as exc:
            logger.debug("CanvasClaudeCard: tmux has-session check (benign): {}", exc)
            return False

    def _kill_tmux_session(self) -> None:
        """Kill the stable tmux session in the container.

        Used by new_session() to force a clean restart.  Unlike the old
        pkill approach, this cleanly terminates the tmux session (and with
        it the claude process running inside).
        """
        try:
            _subprocess.run(
                [
                    _DOCKER,
                    "exec",
                    self._effective_container,
                    "tmux",
                    "kill-session",
                    "-t",
                    TMUX_SESSION_NAME,
                ],
                timeout=5,
                capture_output=True,
            )
            logger.debug("CanvasClaudeCard: killed tmux session {} in {}", TMUX_SESSION_NAME, self._effective_container)
        except Exception as exc:
            logger.debug("CanvasClaudeCard: tmux kill-session (benign): {}", exc)

    def _ensure_tmux_session(self) -> None:
        """Set self.cmd to attach or new-session depending on tmux state.

        If a tmux session named ``canvas-claude`` already exists, the PTY
        command attaches to it (resume).  Otherwise, it creates a new tmux
        session running the claude command.
        """
        docker_bin = _DOCKER
        if self._has_tmux_session():
            self.cmd = f"{docker_bin} exec -it {self._effective_container} tmux attach-session -t {TMUX_SESSION_NAME}"
            logger.info("CanvasClaudeCard: attaching to existing tmux session {}", TMUX_SESSION_NAME)
        else:
            self.cmd = (
                f"{docker_bin} exec -it {self._effective_container} "
                f"tmux new-session -s {TMUX_SESSION_NAME} {self._claude_cmd}"
            )
            logger.info("CanvasClaudeCard: creating new tmux session {}", TMUX_SESSION_NAME)

    async def start(self) -> None:
        """Sync mcp_server.py, write MCP config, resolve tmux state, then start the PTY.

        On the *new-session* path we also seed the claude settings file so the
        workspace-trust TUI dialog never fires. On the *attach* path we skip the
        seed (and skip re-writing the MCP config) because the already-running
        claude process inside tmux keeps whatever config/env snapshot it booted
        with — mutating the files on disk would not retroactively fix a broken
        subprocess and could mislead the next fresh-session boot if the file is
        later consulted.
        """
        loop = _asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sync_mcp_server)
        await loop.run_in_executor(None, self._ensure_tmux_session)

        is_new_session = "tmux new-session" in self.cmd
        if is_new_session:
            await loop.run_in_executor(None, self._seed_claude_settings)

        # Structured diagnostic log line: a single place to look when
        # ``canvas · ✘ failed`` recurs. Fields are stable/parseable.
        logger.info(
            "CanvasClaudeCard.start: tmux_path={} mcp_sha256={} python3={} profile={} container={}",
            "new" if is_new_session else "attach",
            self._mcp_sha256,
            CONTAINER_PYTHON3,
            self.profile or "<none>",
            self._effective_container,
        )

        await super().start()

    # ── Session helpers ────────────────────────────────────────────────

    async def new_session(self) -> None:
        """Destroy current PTY and start a fresh claude process.

        Kills the tmux session first so _ensure_tmux_session() creates a
        new one rather than reattaching to the old conversation.
        """
        await self.stop()
        loop = _asyncio.get_running_loop()
        await loop.run_in_executor(None, self._kill_tmux_session)
        await self.start()

    async def clear_session(self) -> None:
        """Send /clear to the claude PTY to wipe conversation history."""
        if self._session is not None and self._session.alive:
            self._session.pty.write("/clear\n")
