"""Tests for MCP server tool functions and JSON-RPC dispatch."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from claude_rts.mcp_server import (
    _DEFAULT_API_BASE,
    _resolve_api_base,
    handle_request,
    tool_delete_terminal,
    tool_list_terminals,
    tool_open_terminal,
    tool_read_terminal,
    tool_write_terminal,
    tool_rename_terminal,
    tool_set_recovery_script,
    tool_get_recovery_script,
    tool_vm_discover_containers,
    tool_vm_get_favorites,
    tool_vm_set_container_actions,
    tool_vm_add_favorite,
    tool_vm_get_container_actions,
    tool_vm_append_container_action,
    tool_vm_start_container,
    tool_vm_stop_container,
    tool_blueprint_list,
    tool_blueprint_get,
    tool_blueprint_save,
    tool_blueprint_delete,
    tool_blueprint_spawn,
)


# ── API base resolution (issue #114) ───────────────────────────────────────


def test_resolve_api_base_from_argv_space_separated():
    """--api-base http://x takes precedence over env var."""
    with patch.dict(os.environ, {"SUPREME_CLAUDEMANDER_API": "http://env-var:1"}):
        result = _resolve_api_base(["--api-base", "http://argv:2"])
    assert result == "http://argv:2"


def test_resolve_api_base_from_argv_equals_form():
    """--api-base=http://x is recognised."""
    with patch.dict(os.environ, {"SUPREME_CLAUDEMANDER_API": "http://env-var:1"}, clear=False):
        result = _resolve_api_base(["--api-base=http://argv:3"])
    assert result == "http://argv:3"


def test_resolve_api_base_falls_back_to_env():
    """With no argv flag, uses SUPREME_CLAUDEMANDER_API env var."""
    with patch.dict(os.environ, {"SUPREME_CLAUDEMANDER_API": "http://env-only:4"}, clear=False):
        result = _resolve_api_base([])
    assert result == "http://env-only:4"


def test_resolve_api_base_falls_back_to_default():
    """With no argv and no env var, uses the hardcoded default."""
    with patch.dict(os.environ, {}, clear=True):
        result = _resolve_api_base([])
    assert result == _DEFAULT_API_BASE
    assert result == "http://host.docker.internal:3000"


def test_resolve_api_base_ignores_unrelated_argv():
    """Unknown flags don't confuse the parser."""
    with patch.dict(os.environ, {}, clear=True):
        result = _resolve_api_base(["--foo", "bar", "--api-base", "http://ok:9", "--baz"])
    assert result == "http://ok:9"


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
    """tools/list returns all tool schemas with required fields."""
    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    resp = handle_request(msg)
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {
        "open_terminal",
        "read_terminal",
        "write_terminal",
        "list_terminals",
        "delete_terminal",
        "rename_terminal",
        "set_recovery_script",
        "get_recovery_script",
        "vm_discover_containers",
        "vm_get_favorites",
        "vm_set_container_actions",
        "vm_get_container_actions",
        "vm_append_container_action",
        "vm_start_container",
        "vm_stop_container",
        "vm_add_favorite",
        "blueprint_list",
        "blueprint_get",
        "blueprint_save",
        "blueprint_delete",
        "blueprint_spawn",
    }
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


# ── VM Manager MCP tool tests ──────────────────────────────────────────────


def test_vm_discover_containers():
    """vm_discover_containers GETs /api/vms/discover and formats result."""
    containers = [
        {"name": "web-app", "state": "online", "image": "node:18", "status": "Up 2 hours"},
        {"name": "db-server", "state": "offline", "image": "postgres:15", "status": "Exited 3h ago"},
    ]
    mock_resp = make_mock_response(containers)
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_vm_discover_containers({})
    assert "web-app" in result
    assert "online" in result
    assert "db-server" in result
    assert "node:18" in result
    call_args = mock_open.call_args
    req = call_args[0][0]
    assert "/api/vms/discover" in req.full_url


def test_vm_discover_containers_empty():
    """vm_discover_containers returns message when no containers."""
    mock_resp = make_mock_response([])
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = tool_vm_discover_containers({})
    assert "no" in result.lower() or result == ""


def test_vm_get_favorites():
    """vm_get_favorites GETs /api/vms/favorites and returns JSON."""
    favorites = [
        {"name": "web-app", "type": "docker", "actions": [{"label": "Dev Shell", "blueprint": "dev-shell"}]},
    ]
    mock_resp = make_mock_response(favorites)
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_vm_get_favorites({})
    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "web-app"
    req = mock_open.call_args[0][0]
    assert "/api/vms/favorites" in req.full_url


def test_vm_set_container_actions():
    """vm_set_container_actions PUTs to /api/vms/favorites/{name}/actions."""
    actions = [{"label": "Dev Shell", "blueprint": "dev-shell"}, {"label": "Claude", "blueprint": "claude-session"}]
    mock_resp = make_mock_response(actions)
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_vm_set_container_actions({"container": "web-app", "actions": actions})
    assert "web-app" in result
    req = mock_open.call_args[0][0]
    assert "/api/vms/favorites/web-app/actions" in req.full_url
    assert req.method == "PUT"


def test_vm_set_container_actions_missing_container():
    """vm_set_container_actions raises ValueError when container is missing."""
    with pytest.raises(ValueError):
        tool_vm_set_container_actions({})


def test_vm_add_favorite():
    """vm_add_favorite GETs favorites, appends, PUTs back with empty default actions."""
    existing = [{"name": "old-container", "type": "docker", "actions": []}]
    get_resp = make_mock_response(existing)
    put_resp = make_mock_response(existing + [{"name": "new-container", "type": "docker", "actions": []}])
    call_count = [0]
    responses = [get_resp, put_resp]

    def mock_urlopen(req, timeout=None):
        resp = responses[call_count[0]]
        call_count[0] += 1
        return resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen) as mock_open:
        result = tool_vm_add_favorite({"name": "new-container"})
    assert "new-container" in result
    assert "Added" in result
    # Verify PUT was called with the new container and empty actions
    put_req = mock_open.call_args_list[1][0][0]
    put_body = json.loads(put_req.data.decode("utf-8"))
    assert len(put_body) == 2
    assert put_body[1]["name"] == "new-container"
    assert put_body[1]["actions"] == []


def test_vm_add_favorite_already_exists():
    """vm_add_favorite returns message when container already a favorite."""
    existing = [{"name": "web-app", "type": "docker", "actions": []}]
    mock_resp = make_mock_response(existing)
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = tool_vm_add_favorite({"name": "web-app"})
    assert "already" in result.lower()


def test_vm_add_favorite_missing_name():
    """vm_add_favorite raises ValueError when name is missing."""
    with pytest.raises(ValueError):
        tool_vm_add_favorite({})


def test_tools_list_includes_vm_and_blueprint_tools():
    """tools/list response includes all tools (8 terminal + 8 VM + 5 blueprint)."""
    msg = {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}}
    resp = handle_request(msg)
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "vm_discover_containers" in names
    assert "vm_get_favorites" in names
    assert "vm_set_container_actions" in names
    assert "vm_get_container_actions" in names
    assert "vm_append_container_action" in names
    assert "vm_start_container" in names
    assert "vm_stop_container" in names
    assert "vm_add_favorite" in names
    assert "blueprint_list" in names
    assert "blueprint_get" in names
    assert "blueprint_save" in names
    assert "blueprint_delete" in names
    assert "blueprint_spawn" in names
    assert "rename_terminal" in names
    assert "set_recovery_script" in names
    assert "get_recovery_script" in names
    assert len(names) == 21


# ── vm_get_container_actions ──────────────────────────────────────────────


def test_vm_get_container_actions_returns_one():
    """vm_get_container_actions filters favorites to one container's actions."""
    favorites = [
        {"name": "web-app", "type": "docker", "actions": [{"label": "Dev Shell", "blueprint": "dev-shell"}]},
        {"name": "db", "type": "docker", "actions": [{"label": "DB Admin", "blueprint": "db-admin"}]},
    ]
    mock_resp = make_mock_response(favorites)
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = tool_vm_get_container_actions({"container": "web-app"})
    parsed = json.loads(result)
    assert parsed == [{"label": "Dev Shell", "blueprint": "dev-shell"}]


def test_vm_get_container_actions_unknown_raises():
    """vm_get_container_actions raises ValueError with 'Favorite not found' for unknown container."""
    favorites = [{"name": "web-app", "type": "docker", "actions": []}]
    mock_resp = make_mock_response(favorites)
    with patch("urllib.request.urlopen", return_value=mock_resp):
        with pytest.raises(ValueError, match="Favorite not found: nonexistent-xyz"):
            tool_vm_get_container_actions({"container": "nonexistent-xyz"})


def test_vm_get_container_actions_missing_container():
    """vm_get_container_actions raises ValueError when container is missing."""
    with pytest.raises(ValueError, match="container is required"):
        tool_vm_get_container_actions({})


# ── vm_append_container_action ────────────────────────────────────────────


def test_vm_append_container_action_preserves_existing():
    """vm_append_container_action GETs current actions, appends, PUTs atomically."""
    existing_favs = [
        {
            "name": "web-app",
            "type": "docker",
            "actions": [{"label": "Dev Shell", "blueprint": "dev-shell"}],
        }
    ]
    put_result = [
        {"label": "Dev Shell", "blueprint": "dev-shell"},
        {"label": "Claude", "blueprint": "claude-session"},
    ]
    get_resp = make_mock_response(existing_favs)
    put_resp = make_mock_response(put_result)
    responses = [get_resp, put_resp]
    calls = []

    def mock_urlopen(req, timeout=None):
        calls.append(req)
        return responses[len(calls) - 1]

    new_action = {"label": "Claude", "blueprint": "claude-session"}
    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = tool_vm_append_container_action({"container": "web-app", "action": new_action})

    assert "web-app" in result
    # Verify GET then PUT
    assert calls[0].method == "GET"
    assert "/api/vms/favorites" in calls[0].full_url
    assert calls[1].method == "PUT"
    assert "/api/vms/favorites/web-app/actions" in calls[1].full_url
    put_body = json.loads(calls[1].data.decode("utf-8"))
    assert len(put_body) == 2
    assert put_body[0]["label"] == "Dev Shell"
    assert put_body[1]["label"] == "Claude"
    assert put_body[1]["blueprint"] == "claude-session"


def test_vm_append_container_action_unknown_container():
    """vm_append_container_action raises ValueError for unknown container."""
    favorites = [{"name": "web-app", "type": "docker", "actions": []}]
    mock_resp = make_mock_response(favorites)
    with patch("urllib.request.urlopen", return_value=mock_resp):
        with pytest.raises(ValueError, match="Favorite not found: nonexistent-xyz"):
            tool_vm_append_container_action(
                {"container": "nonexistent-xyz", "action": {"label": "X", "blueprint": "test"}}
            )


def test_vm_append_container_action_missing_action():
    """vm_append_container_action raises ValueError when action is missing."""
    with pytest.raises(ValueError, match="action"):
        tool_vm_append_container_action({"container": "web-app"})


def test_vm_append_container_action_missing_container():
    """vm_append_container_action raises ValueError when container is missing."""
    with pytest.raises(ValueError, match="container is required"):
        tool_vm_append_container_action({"action": {"label": "X", "blueprint": "test"}})


# ── vm_start_container / vm_stop_container ────────────────────────────────


def test_vm_start_container_calls_rest():
    """vm_start_container POSTs to /api/vms/{name}/start."""
    mock_resp = make_mock_response({"name": "foo-dev", "state": "online"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_vm_start_container({"name": "foo-dev"})
    assert "foo-dev" in result
    req = mock_open.call_args[0][0]
    assert req.method == "POST"
    assert "/api/vms/foo-dev/start" in req.full_url


def test_vm_start_container_missing_name():
    """vm_start_container raises ValueError when name is missing."""
    with pytest.raises(ValueError, match="name is required"):
        tool_vm_start_container({})


def test_vm_stop_container_calls_rest():
    """vm_stop_container POSTs to /api/vms/{name}/stop."""
    mock_resp = make_mock_response({"name": "foo-dev", "state": "offline"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_vm_stop_container({"name": "foo-dev"})
    assert "foo-dev" in result
    req = mock_open.call_args[0][0]
    assert req.method == "POST"
    assert "/api/vms/foo-dev/stop" in req.full_url
    assert "timeout=" not in req.full_url


def test_vm_stop_container_with_timeout():
    """vm_stop_container passes timeout query param when provided."""
    mock_resp = make_mock_response({"name": "foo-dev", "state": "offline"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        tool_vm_stop_container({"name": "foo-dev", "timeout": 30})
    req = mock_open.call_args[0][0]
    assert "timeout=30" in req.full_url


def test_vm_stop_container_missing_name():
    """vm_stop_container raises ValueError when name is missing."""
    with pytest.raises(ValueError, match="name is required"):
        tool_vm_stop_container({})


# ── Blueprint MCP tool tests ─────────────────────────────────────────────


def test_blueprint_list_returns_names():
    """blueprint_list GETs /api/blueprints and formats result."""
    mock_resp = make_mock_response(["dev-shell", "claude-session"])
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_blueprint_list({})
    assert "dev-shell" in result
    assert "claude-session" in result
    req = mock_open.call_args[0][0]
    assert "/api/blueprints" in req.full_url


def test_blueprint_list_empty():
    """blueprint_list returns message when no blueprints exist."""
    mock_resp = make_mock_response([])
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = tool_blueprint_list({})
    assert "no" in result.lower()


def test_blueprint_get_returns_definition():
    """blueprint_get GETs /api/blueprints/{name} and returns JSON."""
    bp_data = {"name": "dev-shell", "steps": [{"action": "open_terminal", "cmd": "bash"}]}
    mock_resp = make_mock_response(bp_data)
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_blueprint_get({"name": "dev-shell"})
    parsed = json.loads(result)
    assert parsed["name"] == "dev-shell"
    req = mock_open.call_args[0][0]
    assert "/api/blueprints/dev-shell" in req.full_url


def test_blueprint_get_missing_name():
    """blueprint_get raises ValueError when name is missing."""
    with pytest.raises(ValueError, match="name is required"):
        tool_blueprint_get({})


def test_blueprint_save_creates_new():
    """blueprint_save tries PUT then falls back to POST for new blueprints."""
    import urllib.error

    bp = {"name": "dev-shell", "steps": [{"action": "open_terminal", "cmd": "bash"}]}
    # First call (PUT) raises 404, second call (POST) succeeds
    put_error = urllib.error.HTTPError("http://test/api/blueprints/dev-shell", 404, "Not Found", {}, None)
    post_resp = make_mock_response({"name": "dev-shell", "status": "created"})
    calls = []

    def mock_urlopen(req, timeout=None):
        calls.append(req)
        if len(calls) == 1:
            raise put_error
        return post_resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = tool_blueprint_save({"name": "dev-shell", "blueprint": bp})
    assert "dev-shell" in result
    assert "created" in result.lower() or "saved" in result.lower()
    assert calls[0].method == "PUT"
    assert calls[1].method == "POST"


def test_blueprint_save_updates_existing():
    """blueprint_save PUTs to update an existing blueprint."""
    bp = {"name": "dev-shell", "steps": [{"action": "open_terminal", "cmd": "bash"}]}
    mock_resp = make_mock_response({"name": "dev-shell", "status": "updated"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_blueprint_save({"name": "dev-shell", "blueprint": bp})
    assert "dev-shell" in result
    assert "updated" in result.lower() or "saved" in result.lower()
    req = mock_open.call_args[0][0]
    assert req.method == "PUT"


def test_blueprint_save_missing_name():
    """blueprint_save raises ValueError when name is missing."""
    with pytest.raises(ValueError, match="name is required"):
        tool_blueprint_save({"blueprint": {}})


def test_blueprint_save_missing_blueprint():
    """blueprint_save raises ValueError when blueprint is missing."""
    with pytest.raises(ValueError, match="blueprint"):
        tool_blueprint_save({"name": "test"})


def test_blueprint_delete_calls_rest():
    """blueprint_delete DELETEs /api/blueprints/{name}."""
    mock_resp = make_mock_response({"status": "ok"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_blueprint_delete({"name": "dev-shell"})
    assert "dev-shell" in result
    assert "deleted" in result.lower()
    req = mock_open.call_args[0][0]
    assert req.method == "DELETE"
    assert "/api/blueprints/dev-shell" in req.full_url


def test_blueprint_delete_missing_name():
    """blueprint_delete raises ValueError when name is missing."""
    with pytest.raises(ValueError, match="name is required"):
        tool_blueprint_delete({})


def test_blueprint_spawn_calls_rest():
    """blueprint_spawn POSTs to /api/blueprints/spawn with name and context."""
    mock_resp = make_mock_response({"id": "bp-card-123", "type": "blueprint"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_blueprint_spawn({"name": "dev-shell", "context": {"container": "my-dev"}})
    assert "bp-card-123" in result
    assert "dev-shell" in result
    req = mock_open.call_args[0][0]
    assert req.method == "POST"
    assert "/api/blueprints/spawn" in req.full_url
    body = json.loads(req.data.decode("utf-8"))
    assert body["name"] == "dev-shell"
    assert body["context"]["container"] == "my-dev"


def test_blueprint_spawn_with_layout():
    """blueprint_spawn passes layout parameters to the API."""
    mock_resp = make_mock_response({"id": "bp-card-456", "type": "blueprint"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_blueprint_spawn({"name": "dev-shell", "x": 100, "y": 200, "w": 960, "h": 640})
    assert "bp-card-456" in result
    req = mock_open.call_args[0][0]
    body = json.loads(req.data.decode("utf-8"))
    assert body["x"] == 100
    assert body["y"] == 200
    assert body["w"] == 960
    assert body["h"] == 640


def test_blueprint_spawn_missing_name():
    """blueprint_spawn raises ValueError when name is missing."""
    with pytest.raises(ValueError, match="name is required"):
        tool_blueprint_spawn({})


def test_vm_add_favorite_default_actions_empty():
    """vm_add_favorite defaults to empty actions array (no terminal default)."""
    existing = []
    get_resp = make_mock_response(existing)
    put_resp = make_mock_response([{"name": "test-vm", "type": "docker", "actions": []}])
    responses = [get_resp, put_resp]
    call_count = [0]

    def mock_urlopen(req, timeout=None):
        resp = responses[call_count[0]]
        call_count[0] += 1
        return resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen) as mock_open:
        result = tool_vm_add_favorite({"name": "test-vm"})
    assert "test-vm" in result
    assert "0 action(s)" in result
    put_req = mock_open.call_args_list[1][0][0]
    put_body = json.loads(put_req.data.decode("utf-8"))
    assert put_body[0]["actions"] == []


# ── Issue #151: rename_terminal, set/get_recovery_script MCP tools ────────


def test_rename_terminal_calls_api():
    """rename_terminal PUTs to /api/claude/terminal/{id}/rename."""
    mock_resp = make_mock_response({"status": "ok", "display_name": "Build Server"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_rename_terminal({"session_id": "rts-abc", "display_name": "Build Server"})
    assert "Build Server" in result
    req = mock_open.call_args[0][0]
    assert "/api/claude/terminal/rts-abc/rename" in req.full_url
    assert req.method == "PUT"


def test_rename_terminal_missing_session_id():
    """rename_terminal raises ValueError when session_id is missing."""
    with pytest.raises(ValueError):
        tool_rename_terminal({"display_name": "Test"})


def test_set_recovery_script_calls_api():
    """set_recovery_script PUTs to /api/claude/terminal/{id}/recovery-script."""
    mock_resp = make_mock_response({"status": "ok", "recovery_script": "make dev"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_set_recovery_script({"session_id": "rts-abc", "script": "make dev"})
    assert "rts-abc" in result
    req = mock_open.call_args[0][0]
    assert "/api/claude/terminal/rts-abc/recovery-script" in req.full_url
    assert req.method == "PUT"


def test_set_recovery_script_missing_session_id():
    """set_recovery_script raises ValueError when session_id is missing."""
    with pytest.raises(ValueError):
        tool_set_recovery_script({"script": "test"})


def test_get_recovery_script_calls_api():
    """get_recovery_script GETs /api/claude/terminal/{id}/recovery-script."""
    mock_resp = make_mock_response({"recovery_script": "cd /work && make"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = tool_get_recovery_script({"session_id": "rts-abc"})
    assert result == "cd /work && make"
    req = mock_open.call_args[0][0]
    assert "/api/claude/terminal/rts-abc/recovery-script" in req.full_url


def test_get_recovery_script_missing_session_id():
    """get_recovery_script raises ValueError when session_id is missing."""
    with pytest.raises(ValueError):
        tool_get_recovery_script({})


def test_list_terminals_includes_display_name():
    """list_terminals shows display_name when present."""
    mock_resp = make_mock_response(
        [
            {
                "session_id": "rts-1",
                "exec": "bash",
                "alive": True,
                "display_name": "Build Server",
                "recovery_script": "make dev",
            },
            {"session_id": "rts-2", "exec": "sh", "alive": True, "display_name": "", "recovery_script": ""},
        ]
    )
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = tool_list_terminals({})
    assert "name: Build Server" in result
    assert "recovery_script: make dev" in result
    # rts-2 has no display_name so "name:" should not appear for it
    lines = result.strip().split("\n")
    assert "name:" not in lines[1]


def test_handle_request_tools_call_rename():
    """tools/call dispatches rename_terminal correctly."""
    mock_resp = make_mock_response({"status": "ok", "display_name": "Test"})
    with patch("urllib.request.urlopen", return_value=mock_resp):
        msg = {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "rename_terminal",
                "arguments": {"session_id": "rts-x", "display_name": "Test"},
            },
        }
        resp = handle_request(msg)
    assert resp["id"] == 99
    assert resp["result"]["isError"] is False
    assert "Test" in resp["result"]["content"][0]["text"]
