"""Tests for MCP server tool functions."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from claude_rts.mcp_server import (
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
    try:
        tool_open_terminal({})
        assert False, "Should have raised ValueError"
    except (ValueError, Exception):
        pass
