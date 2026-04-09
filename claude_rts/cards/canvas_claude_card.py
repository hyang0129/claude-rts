"""CanvasClaudeCard: runs `claude` CLI with the canvas MCP server inside the util container."""

import json as _json
import re as _re

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

        # Build the MCP config JSON
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

        # Build the bash command that writes the config file and launches claude
        bash_inner = f"cat > /tmp/mcp.json << 'MCPEOF'\n{mcp_json}\nMCPEOF\n"
        if profile:
            bash_inner += f"CLAUDE_CONFIG_DIR=/profiles/{profile} claude --mcp-config /tmp/mcp.json"
        else:
            bash_inner += "claude --mcp-config /tmp/mcp.json"

        docker_bin = "docker.exe"  # Windows host
        cmd = f"{docker_bin} exec -it {effective_container} bash -c '{bash_inner}'"

        super().__init__(
            session_manager,
            cmd=cmd,
            hub=hub,
            container=container,
            card_id=card_id,
            layout=layout,
        )

        self.api_base_url = api_base_url
        self.profile = profile
        self.canvas_name = canvas_name

    # ── Descriptor serialization ───────────────────────────────────────

    def to_descriptor(self) -> dict:
        """Return descriptor with canvas_claude type and extra fields."""
        desc = super().to_descriptor()
        desc["type"] = "canvas_claude"
        desc["profile"] = self.profile
        desc["canvas_name"] = self.canvas_name
        return desc

    # ── Session helpers ────────────────────────────────────────────────

    async def new_session(self) -> None:
        """Destroy current PTY and start a fresh claude process."""
        await self.stop()
        await self.start()

    async def clear_session(self) -> None:
        """Send /clear to the claude PTY to wipe conversation history."""
        if self._session is not None and self._session.alive:
            self._session.pty.write("/clear\n")
