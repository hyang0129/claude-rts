"""CanvasClaudeCard: runs `claude` CLI with the canvas MCP server inside the util container."""

import asyncio as _asyncio
import base64 as _b64
import json as _json
import re as _re
import subprocess as _subprocess

from loguru import logger
from .terminal_card import TerminalCard

# Only alphanumeric, hyphens, and underscores are safe for shell interpolation.
_SAFE_NAME = _re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_name(value: str, label: str) -> None:
    """Raise ValueError if value contains characters unsafe for shell interpolation."""
    if not _SAFE_NAME.match(value):
        raise ValueError(f"Invalid {label} {value!r}: only alphanumeric, hyphens, and underscores are allowed")


class CanvasClaudeCard(TerminalCard):
    """Card that launches the Claude Code CLI with an MCP server exposing canvas tools.

    The MCP server (mcp_server.py) is written into the util container at
    /home/util/mcp_server.py and speaks the MCP stdio protocol over the
    subprocess's stdin/stdout, giving claude access to canvas control tools.

    Start sequence:
    1. write_mcp_config() — writes /tmp/mcp.json into the container via `docker exec`
       (non-interactive subprocess, no PTY, avoids shell-quoting issues on Windows).
    2. The PTY is then started with:
       `docker exec -it <container> env CLAUDE_CONFIG_DIR=/profiles/<profile> claude --mcp-config /tmp/mcp.json`
       This mirrors the working pattern used by ClaudeUsageCard (no bash -c wrapper).
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

        # Build the MCP config JSON and base64-encode it for safe transfer.
        mcp_config = {
            "mcpServers": {
                "canvas": {
                    "command": "python3",
                    "args": ["/home/util/mcp_server.py"],
                    "env": {"SUPREME_CLAUDEMANDER_API": api_base_url},
                }
            }
        }
        mcp_json = _json.dumps(mcp_config)
        self._mcp_b64 = _b64.b64encode(mcp_json.encode()).decode()

        # Build the PTY command — no bash -c wrapper.
        # Using `docker exec -it container env VAR=value claude ...` is identical to
        # the pattern used by ClaudeUsageCard probe sessions, which work reliably
        # through pywinpty/ConPTY on Windows.  The bash -c "..." wrapper interferes
        # with PTY allocation and causes claude to exit immediately.
        docker_bin = "docker.exe"  # Windows host
        if profile:
            cmd = (
                f"{docker_bin} exec -it {effective_container} "
                f"env CLAUDE_CONFIG_DIR=/profiles/{profile} "
                f"claude --mcp-config /tmp/mcp.json"
            )
        else:
            cmd = f"{docker_bin} exec -it {effective_container} claude --mcp-config /tmp/mcp.json"

        # Pass container=None so SessionManager doesn't override cmd with tmux.
        # The docker exec is already baked into cmd; tmux would replace it with bash.
        super().__init__(
            session_manager,
            cmd=cmd,
            hub=hub,
            container=None,
            card_id=card_id,
            layout=layout,
        )

        self._effective_container = effective_container
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

    def _write_mcp_config(self) -> None:
        """Write /tmp/mcp.json into the util container via non-interactive docker exec.

        Uses base64 so no shell-quoting issues regardless of JSON content.
        Runs synchronously; call from an executor when inside async context.
        """
        write_cmd = [
            "docker.exe",
            "exec",
            self._effective_container,
            "bash",
            "-c",
            f"echo {self._mcp_b64} | base64 -d > /tmp/mcp.json",
        ]
        result = _subprocess.run(write_cmd, timeout=10, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to write MCP config: {result.stderr.decode(errors='replace')}")
        logger.debug("CanvasClaudeCard: wrote /tmp/mcp.json to {}", self._effective_container)

    def _kill_orphan_claudes(self) -> None:
        """Kill lingering claude processes in the util container.

        When the Windows-side ConPTY dies, docker exec exits but the container-side
        claude process continues running (Docker-on-Windows doesn't reliably send SIGHUP
        to the container process when the exec connection drops). This cleans up before
        starting a fresh session so orphans don't accumulate.

        NOTE: `pkill -x claude` kills ALL processes named 'claude' in the container.
        Only one CanvasClaudeCard per container is supported. Running a second card
        against the same container will kill the first card's claude process when the
        second card starts a new session.
        """
        try:
            _subprocess.run(
                ["docker.exe", "exec", self._effective_container, "pkill", "-x", "claude"],
                timeout=5,
                capture_output=True,
            )
            logger.debug("CanvasClaudeCard: killed orphan claude processes in {}", self._effective_container)
        except Exception as exc:
            logger.debug("CanvasClaudeCard: pkill claude (benign): {}", exc)

    async def start(self) -> None:
        """Write MCP config and kill orphans, then start the PTY."""
        loop = _asyncio.get_running_loop()
        await loop.run_in_executor(None, self._kill_orphan_claudes)
        await loop.run_in_executor(None, self._write_mcp_config)
        await super().start()

    # ── Session helpers ────────────────────────────────────────────────

    async def new_session(self) -> None:
        """Destroy current PTY and start a fresh claude process."""
        await self.stop()
        await self.start()

    async def clear_session(self) -> None:
        """Send /clear to the claude PTY to wipe conversation history."""
        if self._session is not None and self._session.alive:
            self._session.pty.write("/clear\n")
