#!/usr/bin/env python3
"""MCP stdio server exposing canvas terminal tools to Claude Code."""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("SUPREME_CLAUDEMANDER_API", "http://host.docker.internal:3000")


def read_message():
    """Read one JSON-RPC message from stdin."""
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.decode("utf-8").strip()
        if line.startswith("Content-Length:"):
            length = int(line.split(":", 1)[1].strip())
        elif line == "":
            break
    if length is None:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def write_message(obj):
    """Write one JSON-RPC message to stdout."""
    body = json.dumps(obj).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    sys.stdout.buffer.write(header + body)
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


TOOL_HANDLERS = {
    "open_terminal": tool_open_terminal,
    "read_terminal": tool_read_terminal,
    "write_terminal": tool_write_terminal,
    "list_terminals": tool_list_terminals,
    "delete_terminal": tool_delete_terminal,
}

TOOL_SCHEMAS = [
    {
        "name": "open_terminal",
        "description": "Open a new terminal card on the canvas and return its session_id",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Command to run in the terminal"},
                "hub": {"type": "string", "description": "Hub name (optional)"},
                "container": {"type": "string", "description": "Docker container name (optional)"},
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
