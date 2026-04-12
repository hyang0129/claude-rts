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
        "description": "Open a new terminal card on the canvas and return its session_id",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd": {
                    "type": "string",
                    "description": (
                        "Command to run. When 'container' is also set, this command runs "
                        "inside that Docker container (the server wraps it in docker exec "
                        "automatically). Example: cmd='bash', container='supreme-claudemander-util' "
                        "opens a bash shell inside that container. To run claude inside a "
                        "container: cmd='claude --dangerously-skip-permissions', "
                        "container='supreme-claudemander-util'."
                    ),
                },
                "hub": {"type": "string", "description": "Hub name (optional)"},
                "container": {
                    "type": "string",
                    "description": (
                        "Exact Docker container name to connect to. Use vm_discover_containers "
                        "to find the correct name (e.g. 'supreme-claudemander-util'). "
                        "When set, cmd runs inside this container."
                    ),
                },
                "x": {"type": "number", "description": "X position on canvas (optional)"},
                "y": {"type": "number", "description": "Y position on canvas (optional)"},
                "w": {"type": "number", "description": "Width in pixels (optional)"},
                "h": {"type": "number", "description": "Height in pixels (optional)"},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "read_terminal",
        "description": "Read the current output (scrollback) of a terminal card",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID of the terminal to read"},
                "last_n": {
                    "type": "integer",
                    "description": "Only return the last N bytes of output (optional, default: full scrollback)",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "write_terminal",
        "description": "Write text to a terminal card (as keyboard input)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID of the terminal"},
                "text": {"type": "string", "description": "Text to send to the terminal"},
            },
            "required": ["session_id", "text"],
        },
    },
    {
        "name": "list_terminals",
        "description": "List all active terminal cards on the canvas",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "delete_terminal",
        "description": "Delete a terminal card from the canvas",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID of the terminal to delete"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "vm_discover_containers",
        "description": "Discover all Docker containers (running + stopped) with name, state, image, and status",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "vm_get_favorites",
        "description": "Get the current VM Manager favorites list with all actions and metadata. Each favorite has: name (string), type ('docker'), actions (array of action objects). Action schema: {label: string, type: 'terminal', shell_prefix?: string, import_keys?: string[]}",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "vm_set_container_actions",
        "description": "Update the actions array for a specific favorite container. Action schema: {label: string, type: 'terminal', shell_prefix?: string (command prefix to run in container), import_keys?: string[] (config keys to interpolate, e.g. 'priority_credential')}",
        "inputSchema": {
            "type": "object",
            "properties": {
                "container": {"type": "string", "description": "Name of the favorite container to update"},
                "actions": {
                    "type": "array",
                    "description": "Array of action objects: [{label, type, shell_prefix?, import_keys?}]",
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
        "description": "Get the actions array for a single favorite container. Returns JSON array of action objects. Errors if the container is not in favorites. Use this to inspect current actions before appending or replacing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "container": {"type": "string", "description": "Name of the favorite container"},
            },
            "required": ["container"],
        },
    },
    {
        "name": "vm_append_container_action",
        "description": "Atomically append a single action to a favorite container's actions array (read-modify-write). Safer than vm_set_container_actions when you only want to add one entry without dropping existing actions. Action schema: {label: string, type: 'terminal', shell_prefix?: string, import_keys?: string[]}",
        "inputSchema": {
            "type": "object",
            "properties": {
                "container": {"type": "string", "description": "Name of the favorite container"},
                "action": {
                    "type": "object",
                    "description": "Single action object to append",
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
        "description": "Start a stopped Docker container by name (calls POST /api/vms/{name}/start). Returns the new state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the Docker container to start"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "vm_stop_container",
        "description": "Stop a running Docker container by name (calls POST /api/vms/{name}/stop). Optional timeout (seconds) before forced kill.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the Docker container to stop"},
                "timeout": {"type": "integer", "description": "Optional seconds to wait before forced kill"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "vm_add_favorite",
        "description": "Add a container to the VM Manager favorites list. If the container is already a favorite, returns a message indicating so. Action schema: {label: string, type: 'terminal', shell_prefix?: string, import_keys?: string[]}",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the Docker container to add"},
                "actions": {
                    "type": "array",
                    "description": "Array of action objects (default: [{label: 'Terminal', type: 'terminal'}])",
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
