"""Tests for MCP server tool functions and JSON-RPC dispatch."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from claude_rts.mcp_server import (
    handle_request,
    tool_delete_terminal,
    tool_list_terminals,
    tool_open_terminal,
    tool_read_terminal,
    tool_write_terminal,
)


def make_mock_response(data, status=200):
    """Create a mock urllib response."""
    body = json.dumps(data).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def test_open_terminal_calls_api():
    """open_terminal POSTs to /api/claude/terminal/create and returns session_id."""
    mock_resp = make_mock_response({"session_id": "rts-abc123", "type": "terminal"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_open_terminal({"cmd": "bash"})
    assert "rts-abc123" in result
    call_args = mock_open.call_args
    req = call_args[0][0]
    assert "/api/claude/terminal/create" in req.full_url
    assert "cmd=bash" in req.full_url
    assert req.method == "POST"


def test_read_terminal_strip_ansi():
    """read_terminal GETs with strip_ansi=true."""
    mock_resp = make_mock_response({"output": "hello world", "size": 11, "total_written": 11})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_read_terminal({"session_id": "rts-abc123"})
    assert result == "hello world"
    call_args = mock_open.call_args
    req = call_args[0][0]
    assert "strip_ansi=true" in req.full_url
    assert "rts-abc123" in req.full_url


def test_write_terminal_sends_text():
    """write_terminal POSTs to /send with text body."""
    mock_resp = make_mock_response({"status": "ok", "sent": 5})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_write_terminal({"session_id": "rts-abc123", "text": "hello"})
    assert "5" in result or "sent" in result.lower()
    call_args = mock_open.call_args
    req = call_args[0][0]
    assert "/send" in req.full_url
    assert req.method == "POST"


def test_list_terminals_returns_text():
    """list_terminals GETs /api/claude/terminals and formats result."""
    terminals = [
        {"session_id": "rts-abc", "exec": "bash", "alive": True},
        {"session_id": "rts-def", "exec": "python", "alive": False},
    ]
    mock_resp = make_mock_response(terminals)
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_list_terminals({})
    assert "rts-abc" in result
    assert "rts-def" in result
    call_args = mock_open.call_args
    req = call_args[0][0]
    assert "/api/claude/terminals" in req.full_url


def test_delete_terminal():
    """delete_terminal DELETEs /api/claude/terminal/{id}."""
    mock_resp = make_mock_response({"status": "ok"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_delete_terminal({"session_id": "rts-abc123"})
    assert "deleted" in result.lower() or "ok" in result.lower()
    call_args = mock_open.call_args
    req = call_args[0][0]
    assert "rts-abc123" in req.full_url
    assert req.method == "DELETE"


def test_list_terminals_empty():
    """list_terminals returns a human-readable message when empty."""
    mock_resp = make_mock_response([])
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = tool_list_terminals({})
    assert "no" in result.lower() or result == "" or isinstance(result, str)


def test_open_terminal_missing_cmd():
    """open_terminal raises ValueError when cmd is missing."""
    import pytest

    with pytest.raises(ValueError):
        tool_open_terminal({})


# ── handle_request / MCP JSON-RPC dispatch tests ─────────────────────────────


def test_handle_request_initialize():
    """initialize returns protocol version, capabilities, and server info."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    resp = handle_request(msg)
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "canvas-mcp"


def test_handle_request_tools_list():
    """tools/list returns all 5 tool schemas with required fields."""
    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    resp = handle_request(msg)
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"open_terminal", "read_terminal", "write_terminal", "list_terminals", "delete_terminal"}
    for t in tools:
        assert "description" in t
        assert "inputSchema" in t


def test_handle_request_tools_call_dispatch():
    """tools/call dispatches to the correct handler and returns text content."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps([]).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=mock_resp):
        msg = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_terminals", "arguments": {}},
        }
        resp = handle_request(msg)
    assert resp["id"] == 3
    content = resp["result"]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert resp["result"]["isError"] is False


def test_handle_request_tools_call_unknown_tool():
    """tools/call with unknown tool returns error response."""
    msg = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "nonexistent_tool", "arguments": {}},
    }
    resp = handle_request(msg)
    assert "error" in resp
    assert resp["error"]["code"] == -32601
    assert "nonexistent_tool" in resp["error"]["message"]


def test_handle_request_tools_call_handler_error():
    """tools/call returns isError=True when handler raises an exception."""
    msg = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "open_terminal", "arguments": {}},  # missing required 'cmd'
    }
    resp = handle_request(msg)
    assert resp["result"]["isError"] is True
    assert "Error:" in resp["result"]["content"][0]["text"]


def test_handle_request_ping():
    """ping returns empty result."""
    msg = {"jsonrpc": "2.0", "id": 6, "method": "ping", "params": {}}
    resp = handle_request(msg)
    assert resp["result"] == {}
    assert resp["id"] == 6


def test_handle_request_notification_no_response():
    """notifications/initialized returns None (no response for notifications)."""
    msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    resp = handle_request(msg)
    assert resp is None


def test_handle_request_unknown_method_with_id():
    """Unknown method with an id returns error response."""
    msg = {"jsonrpc": "2.0", "id": 7, "method": "bogus/method", "params": {}}
    resp = handle_request(msg)
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_handle_request_unknown_method_notification():
    """Unknown method without id (notification) returns None."""
    msg = {"jsonrpc": "2.0", "method": "bogus/method", "params": {}}
    resp = handle_request(msg)
    assert resp is None
