#!/usr/bin/env python3
"""MCP stdio server exposing canvas terminal tools to Claude Code."""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

_DEFAULT_API_BASE = "http://host.docker.internal:3000"


def _resolve_api_base(argv: list[str] | None = None) -> str:
    """Resolve the API base URL with a three-tier precedence.

    Order (highest precedence first):
      1. ``--api-base <url>`` on argv (or ``--api-base=<url>``)
      2. ``SUPREME_CLAUDEMANDER_API`` environment variable
      3. Hardcoded default (``http://host.docker.internal:3000``)

    Passing the URL via argv lets the parent process (Claude Code) omit the
    ``env`` field entirely so the MCP subprocess inherits a full environment
    (PATH, HOME, etc). The env-var path is retained for backward compatibility
    with any caller that still writes only ``env``.
    """
    args = sys.argv[1:] if argv is None else argv
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--api-base" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--api-base="):
            return a.split("=", 1)[1]
        i += 1
    return os.environ.get("SUPREME_CLAUDEMANDER_API", _DEFAULT_API_BASE)


API_BASE = _resolve_api_base()


def read_message():
    """Read one JSON-RPC message from stdin (NDJSON: one JSON object per line)."""
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError:
        print(f"mcp_server: skipping malformed line: {line[:80]!r}", file=sys.stderr, flush=True)
        return {}


def write_message(obj):
    """Write one JSON-RPC message to stdout (NDJSON: one JSON object per line)."""
    body = json.dumps(obj).encode("utf-8") + b"\n"
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def http_request(method, path, body=None):
    """Make an HTTP request to the claude-rts API."""
    url = API_BASE.rstrip("/") + path
    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "text/plain")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()}")


# ── Tool implementations ───────────────────────────────────────────────────


def tool_open_terminal(args):
    cmd = args.get("cmd", "")
    if not cmd:
        raise ValueError("cmd is required")
    params = f"cmd={urllib.parse.quote(cmd)}"
    for key in ("hub", "container", "x", "y", "w", "h"):
        if key in args and args[key] is not None:
            params += f"&{key}={urllib.parse.quote(str(args[key]))}"
    result = http_request("POST", f"/api/claude/terminal/create?{params}")
    return f"Terminal created. session_id: {result.get('session_id')}"


def tool_read_terminal(args):
    session_id = args.get("session_id", "")
    if not session_id:
        raise ValueError("session_id is required")
    safe_id = urllib.parse.quote(session_id, safe="")
    qs = "strip_ansi=true"
    last_n = args.get("last_n")
    if last_n is not None:
        qs += f"&last_n={int(last_n)}"
    result = http_request("GET", f"/api/claude/terminal/{safe_id}/read?{qs}")
    return result.get("output", "")


def tool_write_terminal(args):
    session_id = args.get("session_id", "")
    text = args.get("text", "")
    if not session_id:
        raise ValueError("session_id is required")
    safe_id = urllib.parse.quote(session_id, safe="")
    result = http_request("POST", f"/api/claude/terminal/{safe_id}/send", body=text)
    return f"Sent {result.get('sent', 0)} bytes"


def tool_list_terminals(args):  # noqa: ARG001
    result = http_request("GET", "/api/claude/terminals")
    if not result:
        return "No active terminals"
    lines = []
    for t in result:
        lines.append(f"- session_id: {t.get('session_id')} cmd: {t.get('exec', '')} alive: {t.get('alive', False)}")
    return "\n".join(lines)


def tool_delete_terminal(args):
    session_id = args.get("session_id", "")
    if not session_id:
        raise ValueError("session_id is required")
    safe_id = urllib.parse.quote(session_id, safe="")
    http_request("DELETE", f"/api/claude/terminal/{safe_id}")
    return "Terminal deleted"


def tool_vm_discover_containers(args):  # noqa: ARG001
    result = http_request("GET", "/api/vms/discover")
    if not isinstance(result, list):
        return f"Error discovering containers: {result}"
    if not result:
        return "No containers found"
    lines = []
    for c in result:
        lines.append(
            f"- {c.get('name')} [{c.get('state', 'unknown')}] image={c.get('image', '')} status={c.get('status', '')}"
        )
    return "\n".join(lines)


def tool_vm_get_favorites(args):  # noqa: ARG001
    result = http_request("GET", "/api/vms/favorites")
    return json.dumps(result, indent=2)


def tool_vm_set_container_actions(args):
    container = args.get("container", "")
    if not container:
        raise ValueError("container is required")
    actions = args.get("actions", [])
    safe_name = urllib.parse.quote(container, safe="")
    result = http_request(
        "PUT",
        f"/api/vms/favorites/{safe_name}/actions",
        body=json.dumps(actions),
    )
    return f"Actions updated for {container}: {json.dumps(result)}"


def tool_vm_get_container_actions(args):
    """Return just one favorite's actions array; error if not found."""
    container = args.get("container", "") or args.get("name", "")
    if not container:
        raise ValueError("container is required")
    favorites = http_request("GET", "/api/vms/favorites")
    for fav in favorites:
        if fav.get("name") == container:
            return json.dumps(fav.get("actions", []), indent=2)
    raise ValueError(f"Favorite not found: {container}")


def tool_vm_append_container_action(args):
    """Atomically append one action to a favorite's actions array."""
    container = args.get("container", "") or args.get("name", "")
    if not container:
        raise ValueError("container is required")
    action = args.get("action")
    if not isinstance(action, dict):
        raise ValueError("action (object) is required")
    favorites = http_request("GET", "/api/vms/favorites")
    target = None
    for fav in favorites:
        if fav.get("name") == container:
            target = fav
            break
    if target is None:
        raise ValueError(f"Favorite not found: {container}")
    existing = list(target.get("actions", []))
    existing.append(action)
    safe_name = urllib.parse.quote(container, safe="")
    result = http_request(
        "PUT",
        f"/api/vms/favorites/{safe_name}/actions",
        body=json.dumps(existing),
    )
    return f"Appended action to {container}: {json.dumps(result)}"


def tool_vm_start_container(args):
    """Start a stopped Docker container via POST /api/vms/{name}/start."""
    name = args.get("name", "") or args.get("container", "")
    if not name:
        raise ValueError("name is required")
    safe_name = urllib.parse.quote(name, safe="")
    result = http_request("POST", f"/api/vms/{safe_name}/start")
    return f"Started {name}: {json.dumps(result)}"


def tool_vm_stop_container(args):
    """Stop a running Docker container via POST /api/vms/{name}/stop."""
    name = args.get("name", "") or args.get("container", "")
    if not name:
        raise ValueError("name is required")
    safe_name = urllib.parse.quote(name, safe="")
    path = f"/api/vms/{safe_name}/stop"
    timeout = args.get("timeout")
    if timeout is not None:
        path += f"?timeout={int(timeout)}"
    result = http_request("POST", path)
    return f"Stopped {name}: {json.dumps(result)}"


def tool_vm_add_favorite(args):
    name = args.get("name", "")
    if not name:
        raise ValueError("name is required")
    actions = args.get("actions", [{"label": "Terminal", "type": "terminal"}])
    # GET current favorites, append, PUT back
    favorites = http_request("GET", "/api/vms/favorites")
    # Check if already exists
    for fav in favorites:
        if fav.get("name") == name:
            return f"Container {name} is already a favorite"
    favorites.append({"name": name, "type": "docker", "actions": actions})
    http_request("PUT", "/api/vms/favorites", body=json.dumps(favorites))
    return f"Added {name} to favorites with {len(actions)} action(s)"


TOOL_HANDLERS = {
    "open_terminal": tool_open_terminal,
    "read_terminal": tool_read_terminal,
    "write_terminal": tool_write_terminal,
    "list_terminals": tool_list_terminals,
    "delete_terminal": tool_delete_terminal,
    "vm_discover_containers": tool_vm_discover_containers,
    "vm_get_favorites": tool_vm_get_favorites,
    "vm_set_container_actions": tool_vm_set_container_actions,
    "vm_get_container_actions": tool_vm_get_container_actions,
    "vm_append_container_action": tool_vm_append_container_action,
    "vm_start_container": tool_vm_start_container,
    "vm_stop_container": tool_vm_stop_container,
    "vm_add_favorite": tool_vm_add_favorite,
}

TOOL_SCHEMAS = [
    {
        "name": "open_terminal",
        "description": (
            "Open a new terminal card on the canvas and return its session_id. "
            "You can control the card's size and position on the canvas using w, h, x, y — "
            "use these to make cards large, small, or place them in specific screen regions. "
            "The canvas is 3840x2160 (4K). To run a command inside a Docker container, set "
            "both 'cmd' and 'container'. The server handles docker exec automatically. "
            "The special placeholder ${priority_credential} in cmd is replaced server-side "
            "with the user's priority profile name (useful for launching claude with a "
            "specific API credential)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd": {
                    "type": "string",
                    "description": (
                        "Command to run in the terminal. When 'container' is also set, this "
                        "command runs inside that Docker container (the server wraps it in "
                        "docker exec automatically). Examples: "
                        "cmd='bash', container='my-dev' opens a bash shell in the container. "
                        "cmd='claude --dangerously-skip-permissions', container='my-dev' "
                        "launches Claude Code inside the container. "
                        "The placeholder ${priority_credential} in cmd is replaced server-side "
                        "with the current priority profile name from config — e.g. "
                        "cmd='claude --profile ${priority_credential}' becomes "
                        "cmd='claude --profile my-key'. If no priority profile is set, the "
                        "placeholder is left as-is and a warning is logged."
                    ),
                },
                "hub": {
                    "type": "string",
                    "description": (
                        "Hub name for devcontainer-based sessions (optional). "
                        "Usually not needed when using 'container' directly."
                    ),
                },
                "container": {
                    "type": "string",
                    "description": (
                        "Exact Docker container name to connect to. When set, cmd runs inside "
                        "this container via docker exec. Use vm_discover_containers to find "
                        "available container names (e.g. 'supreme-claudemander-util'). "
                        "The container must be running — use vm_start_container first if needed."
                    ),
                },
                "x": {
                    "type": "number",
                    "description": (
                        "X position (left edge) of the card on the canvas, in pixels. "
                        "Canvas is 3840px wide. Default: auto-centered. "
                        "Examples: x=0 for left edge, x=1920 for center, x=3120 for right side "
                        "(leaving room for a 720px-wide card)."
                    ),
                },
                "y": {
                    "type": "number",
                    "description": (
                        "Y position (top edge) of the card on the canvas, in pixels. "
                        "Canvas is 2160px tall. Default: auto-centered. "
                        "Examples: y=0 for top edge, y=840 for vertically centered, "
                        "y=1680 for bottom area (leaving room for a 480px-tall card)."
                    ),
                },
                "w": {
                    "type": "number",
                    "description": (
                        "Width of the card in pixels. Default: 720. Minimum: 300. "
                        "Use larger values for wide terminals: w=1200 for a wide card, "
                        "w=1800 for an extra-wide card that spans half the canvas. "
                        "A 'big' or 'large' terminal typically means w=1200, h=800 or larger."
                    ),
                },
                "h": {
                    "type": "number",
                    "description": (
                        "Height of the card in pixels. Default: 480. Minimum: 200. "
                        "Use larger values for tall terminals: h=800 for a tall card, "
                        "h=1200 for a very tall card. Combine with w for a 'big' terminal: "
                        "w=1200, h=800 gives a large card. w=1800, h=1400 fills most of "
                        "the canvas."
                    ),
                },
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "read_terminal",
        "description": (
            "Read the current scrollback output of a terminal card. Returns plain text "
            "with ANSI escape codes stripped by default. Use last_n to read only recent "
            "output (e.g. last_n=2000 for the last 2KB). Useful for checking command "
            "results, monitoring long-running processes, or verifying that a terminal "
            "is ready for input."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID of the terminal to read (returned by open_terminal or list_terminals)",
                },
                "last_n": {
                    "type": "integer",
                    "description": (
                        "Only return the last N bytes of output. Omit to get full scrollback "
                        "(up to 64KB buffer). Use last_n=500 for a quick status check, "
                        "last_n=2000 for recent context."
                    ),
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "write_terminal",
        "description": (
            "Send keystrokes to a terminal card. The text is written as keyboard input "
            "to the terminal's PTY. Include '\\n' at the end to press Enter and execute "
            "a command (e.g. text='ls -la\\n'). You can also send special keys and control "
            "sequences (e.g. text='\\x03' for Ctrl+C to interrupt a running process)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID of the terminal (returned by open_terminal or list_terminals)",
                },
                "text": {
                    "type": "string",
                    "description": (
                        "Text to send as keyboard input. Include '\\n' to press Enter. "
                        "Examples: 'ls -la\\n' runs a command, '\\x03' sends Ctrl+C, "
                        "'exit\\n' closes the shell."
                    ),
                },
            },
            "required": ["session_id", "text"],
        },
    },
    {
        "name": "list_terminals",
        "description": (
            "List all active terminal cards on the canvas with their session IDs, "
            "commands, and alive status. Use this to find session IDs for read_terminal, "
            "write_terminal, or delete_terminal. Returns one line per terminal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "delete_terminal",
        "description": (
            "Close and remove a terminal card from the canvas. Stops the PTY session "
            "and cleans up the card. The terminal is gone permanently — use open_terminal "
            "to create a new one if needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID of the terminal to delete (from open_terminal or list_terminals)",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "vm_discover_containers",
        "description": (
            "List all Docker containers on the host (both running and stopped). "
            "Returns each container's name, state (online/offline), image, and status. "
            "Use the container names from this list as the 'container' or 'name' parameter "
            "in other tools (open_terminal, vm_start_container, vm_add_favorite, etc.)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "vm_get_favorites",
        "description": (
            "Get the VM Manager favorites list — containers the user has bookmarked "
            "for quick access. Each favorite has: name (string), type ('docker'), "
            "actions (array of action objects). Actions define buttons in the UI. "
            "Action schema: {label: string, type: 'terminal', shell_prefix?: string, "
            "import_keys?: string[]}. Use this to see what containers and actions are "
            "already configured before making changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "vm_set_container_actions",
        "description": (
            "REPLACE the entire actions array for a favorite container. WARNING: this "
            "overwrites all existing actions — if you only want to add one action without "
            "losing the others, use vm_append_container_action instead. "
            "Action schema: {label: string, type: 'terminal', shell_prefix?: string "
            "(command prefix run in container), import_keys?: string[] (config keys to "
            "interpolate, e.g. ['priority_credential'] causes ${priority_credential} in "
            "shell_prefix to be replaced with the active profile name)}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "container": {
                    "type": "string",
                    "description": (
                        "Name of the favorite container to update. "
                        "Must already be in the favorites list (use vm_add_favorite first if needed)."
                    ),
                },
                "actions": {
                    "type": "array",
                    "description": (
                        "Complete replacement actions array. All existing actions are removed "
                        "and replaced with this list. Format: [{label, type, shell_prefix?, import_keys?}]"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "type": {"type": "string", "enum": ["terminal"]},
                            "shell_prefix": {"type": "string"},
                            "import_keys": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["label", "type"],
                    },
                },
            },
            "required": ["container", "actions"],
        },
    },
    {
        "name": "vm_get_container_actions",
        "description": (
            "Get the actions array for a single favorite container. Returns a JSON array "
            "of action objects. Errors if the container is not in favorites. Use this to "
            "inspect current actions before appending or replacing. Accepts either "
            "'container' or 'name' as the parameter key (both work identically)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "container": {
                    "type": "string",
                    "description": (
                        "Name of the favorite container (also accepted as 'name'). "
                        "Must be an exact match for a container in the favorites list."
                    ),
                },
            },
            "required": ["container"],
        },
    },
    {
        "name": "vm_append_container_action",
        "description": (
            "Add one action to a favorite container WITHOUT removing existing actions. "
            "This is an atomic read-modify-write: it fetches the current actions, appends "
            "the new one, and saves the updated list in a single operation. Prefer this over "
            "vm_set_container_actions when you only need to add an action — it is safer "
            "because it never drops existing entries. Accepts either 'container' or 'name' "
            "as the parameter key. "
            "Action schema: {label: string, type: 'terminal', shell_prefix?: string, "
            "import_keys?: string[]}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "container": {
                    "type": "string",
                    "description": (
                        "Name of the favorite container (also accepted as 'name'). "
                        "Must already be in the favorites list."
                    ),
                },
                "action": {
                    "type": "object",
                    "description": (
                        "Single action object to append. Example: "
                        "{label: 'Claude', type: 'terminal', "
                        "shell_prefix: 'claude --dangerously-skip-permissions', "
                        "import_keys: ['priority_credential']}"
                    ),
                    "properties": {
                        "label": {"type": "string"},
                        "type": {"type": "string", "enum": ["terminal"]},
                        "shell_prefix": {"type": "string"},
                        "import_keys": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["label", "type"],
                },
            },
            "required": ["container", "action"],
        },
    },
    {
        "name": "vm_start_container",
        "description": (
            "Start a stopped Docker container. Use vm_discover_containers first to check "
            "the container's current state. After starting, you can open a terminal in it "
            "with open_terminal. Accepts either 'name' or 'container' as the parameter key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Name of the Docker container to start (also accepted as 'container'). "
                        "Use the exact name from vm_discover_containers."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "vm_stop_container",
        "description": (
            "Stop a running Docker container. Use vm_discover_containers first to confirm "
            "it is running. Optionally set a timeout (seconds) before the container is "
            "force-killed. Accepts either 'name' or 'container' as the parameter key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Name of the Docker container to stop (also accepted as 'container'). "
                        "Use the exact name from vm_discover_containers."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Seconds to wait before force-killing the container (optional). "
                        "Default: Docker's default (usually 10s). Set higher for graceful "
                        "shutdown of services."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "vm_add_favorite",
        "description": (
            "Add a Docker container to the VM Manager favorites list so it appears in "
            "the sidebar for quick access. If the container is already a favorite, returns "
            "a message and does nothing. Optionally provide custom actions (buttons); "
            "default is a single 'Terminal' action that opens a shell. "
            "Action schema: {label: string, type: 'terminal', shell_prefix?: string, "
            "import_keys?: string[]}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Name of the Docker container to add to favorites. "
                        "Use the exact name from vm_discover_containers."
                    ),
                },
                "actions": {
                    "type": "array",
                    "description": (
                        "Custom actions for this favorite (optional). Default: "
                        "[{label: 'Terminal', type: 'terminal'}]. Example with Claude action: "
                        "[{label: 'Terminal', type: 'terminal'}, "
                        "{label: 'Claude', type: 'terminal', "
                        "shell_prefix: 'claude --dangerously-skip-permissions', "
                        "import_keys: ['priority_credential']}]"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "type": {"type": "string", "enum": ["terminal"]},
                            "shell_prefix": {"type": "string"},
                            "import_keys": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["label", "type"],
                    },
                },
            },
            "required": ["name"],
        },
    },
]


def handle_request(msg):
    """Handle one JSON-RPC request and return a response (or None for notifications)."""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "canvas-mcp", "version": "1.0.0"},
            },
        }
    elif method == "notifications/initialized":
        return None  # notification, no response
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOL_SCHEMAS},
        }
    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }
        try:
            result_text = handler(tool_args)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": result_text}],
                    "isError": False,
                },
            }
        except Exception as e:  # noqa: BLE001
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            }
    elif method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    else:
        if msg_id is not None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }
        return None


def main():
    while True:
        msg = read_message()
        if msg is None:
            break
        response = handle_request(msg)
        if response is not None:
            write_message(response)


if __name__ == "__main__":
    main()
