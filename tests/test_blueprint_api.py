"""Tests for Blueprint CRUD API, validation, and interpolation."""

import pytest

from claude_rts import config
from claude_rts.server import create_app
from claude_rts.blueprint import (
    interpolate_string,
    interpolate_value,
    find_variable_refs,
    validate_blueprint,
    list_blueprints,
    read_blueprint,
    write_blueprint,
    delete_blueprint,
)


# ── Variable interpolation unit tests ────────────────────────────────────────


def test_interpolate_simple():
    """$variable is replaced with its value."""
    result = interpolate_string("hello $name", {"name": "world"})
    assert result == "hello world"


def test_interpolate_multiple():
    """Multiple $variable refs are replaced."""
    result = interpolate_string("$a and $b", {"a": "foo", "b": "bar"})
    assert result == "foo and bar"


def test_interpolate_substring():
    """$variable works as substring inside larger strings."""
    result = interpolate_string("cd /work/$branch && claude", {"branch": "main"})
    assert result == "cd /work/main && claude"


def test_interpolate_dollar_escape():
    """$$ produces a literal $ character."""
    result = interpolate_string("price is $$5", {})
    assert result == "price is $5"


def test_interpolate_dollar_escape_with_var():
    """$$ escape coexists with $variable references."""
    result = interpolate_string("$$HOME is $dir", {"dir": "/home/user"})
    assert result == "$HOME is /home/user"


def test_interpolate_unresolvable():
    """Unresolvable $variable raises KeyError."""
    with pytest.raises(KeyError, match="Unresolvable variable"):
        interpolate_string("$missing", {})


def test_interpolate_value_numeric_field_rejected():
    """$variable in a numeric field raises ValueError."""
    with pytest.raises(ValueError, match="numeric field"):
        interpolate_value("$width", {"width": 100}, field_name="cols")


def test_interpolate_value_dict():
    """interpolate_value recurses into dicts."""
    result = interpolate_value(
        {"cmd": "echo $msg", "count": 5},
        {"msg": "hello"},
    )
    assert result == {"cmd": "echo hello", "count": 5}


def test_interpolate_value_list():
    """interpolate_value recurses into lists."""
    result = interpolate_value(
        ["$a", "$b"],
        {"a": "x", "b": "y"},
    )
    assert result == ["x", "y"]


def test_interpolate_non_string_passthrough():
    """Non-string values pass through unchanged."""
    assert interpolate_value(42, {}) == 42
    assert interpolate_value(None, {}) is None
    assert interpolate_value(True, {}) is True


# ── find_variable_refs unit tests ────────────────────────────────────────────


def test_find_refs_simple():
    refs = find_variable_refs("$foo and $bar")
    assert refs == {"foo", "bar"}


def test_find_refs_escaped():
    """$$ is not treated as a variable reference."""
    refs = find_variable_refs("$$HOME and $dir")
    assert refs == {"dir"}


def test_find_refs_nested():
    refs = find_variable_refs({"cmd": "$x", "list": ["$y", "$z"]})
    assert refs == {"x", "y", "z"}


# ── Blueprint validation unit tests ─────────────────────────────────────────


def test_validate_minimal_valid():
    bp = {
        "name": "test",
        "steps": [{"action": "get_main_profile", "out": "p"}],
    }
    result = validate_blueprint(bp)
    assert result["valid"] is True
    assert result["errors"] == []
    assert len(result["resolved_steps"]) == 1


def test_validate_missing_name():
    bp = {"steps": [{"action": "get_main_profile"}]}
    result = validate_blueprint(bp)
    assert result["valid"] is False
    assert any("name" in e for e in result["errors"])


def test_validate_empty_steps():
    bp = {"name": "test", "steps": []}
    result = validate_blueprint(bp)
    assert result["valid"] is False
    assert any("at least one step" in e for e in result["errors"])


def test_validate_unknown_action():
    bp = {"name": "test", "steps": [{"action": "fly_to_moon"}]}
    result = validate_blueprint(bp)
    assert result["valid"] is False
    assert any("unknown action" in e for e in result["errors"])


def test_validate_unresolvable_ref():
    bp = {
        "name": "test",
        "steps": [{"action": "open_terminal", "cmd": "$missing"}],
    }
    result = validate_blueprint(bp)
    assert result["valid"] is False
    assert any("unresolvable" in e.lower() for e in result["errors"])


def test_validate_numeric_field_with_var():
    bp = {
        "name": "test",
        "steps": [{"action": "open_terminal", "cmd": "bash", "cols": "$width"}],
    }
    result = validate_blueprint(bp)
    assert result["valid"] is False
    assert any("numeric field" in e for e in result["errors"])


def test_validate_with_context():
    bp = {
        "name": "test",
        "parameters": [
            {"name": "branch", "provenance": "user", "type": "string"},
        ],
        "steps": [{"action": "open_terminal", "cmd": "cd $branch"}],
    }
    result = validate_blueprint(bp, context={"branch": "main"})
    assert result["valid"] is True
    assert result["parameters"]["branch"] == "main"


def test_validate_user_param_missing():
    bp = {
        "name": "test",
        "parameters": [
            {"name": "branch", "provenance": "user", "type": "string"},
        ],
        "steps": [{"action": "open_terminal", "cmd": "cd $branch"}],
    }
    result = validate_blueprint(bp)
    assert result["valid"] is False
    assert any("not provided" in e for e in result["errors"])


def test_validate_param_with_default():
    bp = {
        "name": "test",
        "parameters": [
            {"name": "branch", "provenance": "user", "type": "string", "default": "main"},
        ],
        "steps": [{"action": "open_terminal", "cmd": "cd $branch"}],
    }
    result = validate_blueprint(bp)
    assert result["valid"] is True
    assert result["parameters"]["branch"] == "main"


def test_validate_output_var_chain():
    """Output variable from step N is available in step N+1."""
    bp = {
        "name": "test",
        "steps": [
            {"action": "get_main_profile", "out": "cred"},
            {"action": "open_terminal", "cmd": "echo $cred"},
        ],
    }
    result = validate_blueprint(bp)
    assert result["valid"] is True


def test_validate_for_each():
    bp = {
        "name": "test",
        "steps": [
            {"action": "discover_containers", "out": "containers"},
            {
                "action": "for_each",
                "list": "$containers",
                "item_var": "c",
                "steps": [
                    {"action": "start_container", "container": "$c"},
                ],
            },
        ],
    }
    result = validate_blueprint(bp)
    assert result["valid"] is True


# ── Blueprint CRUD unit tests ───────────────────────────────────────────────


def test_crud_write_read_delete(tmp_path):
    app_config = config.load(tmp_path / ".sc")
    bp = {"name": "test-bp", "steps": [{"action": "get_main_profile"}]}

    assert write_blueprint(app_config, "test-bp", bp) is True
    assert read_blueprint(app_config, "test-bp") == bp
    assert "test-bp" in list_blueprints(app_config)

    assert delete_blueprint(app_config, "test-bp") is True
    assert read_blueprint(app_config, "test-bp") is None
    assert "test-bp" not in list_blueprints(app_config)


def test_crud_invalid_name(tmp_path):
    app_config = config.load(tmp_path / ".sc")
    assert write_blueprint(app_config, "bad name!", {}) is False
    assert read_blueprint(app_config, "bad name!") is None
    assert delete_blueprint(app_config, "bad name!") is False


def test_crud_delete_nonexistent(tmp_path):
    app_config = config.load(tmp_path / ".sc")
    assert delete_blueprint(app_config, "nope") is False


# ── Blueprint API endpoint tests ────────────────────────────────────────────


class MockPty:
    def __init__(self):
        self._alive = True

    def isalive(self):
        return self._alive

    def read(self):
        import time

        time.sleep(0.1)
        if not self._alive:
            raise EOFError()
        return ""

    def write(self, text):
        pass

    def setwinsize(self, rows, cols):
        pass

    def terminate(self, force=False):
        self._alive = False

    @classmethod
    def spawn(cls, cmd, dimensions=(24, 80)):
        return cls()


async def test_api_list_empty(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    resp = await client.get("/api/blueprints")
    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_api_create_and_get(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    bp = {"name": "my-bp", "steps": [{"action": "get_main_profile"}]}
    resp = await client.post("/api/blueprints", json=bp)
    assert resp.status == 201

    resp = await client.get("/api/blueprints/my-bp")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "my-bp"


async def test_api_create_duplicate(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    bp = {"name": "dup", "steps": [{"action": "get_main_profile"}]}
    resp = await client.post("/api/blueprints", json=bp)
    assert resp.status == 201
    resp = await client.post("/api/blueprints", json=bp)
    assert resp.status == 409


async def test_api_update(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    bp = {"name": "upd", "steps": [{"action": "get_main_profile"}]}
    await client.post("/api/blueprints", json=bp)

    bp["description"] = "updated"
    resp = await client.put("/api/blueprints/upd", json=bp)
    assert resp.status == 200

    resp = await client.get("/api/blueprints/upd")
    data = await resp.json()
    assert data["description"] == "updated"


async def test_api_delete(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    bp = {"name": "del-me", "steps": [{"action": "get_main_profile"}]}
    await client.post("/api/blueprints", json=bp)

    resp = await client.delete("/api/blueprints/del-me")
    assert resp.status == 200

    resp = await client.get("/api/blueprints/del-me")
    assert resp.status == 404


async def test_api_delete_not_found(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    resp = await client.delete("/api/blueprints/nope")
    assert resp.status == 404


async def test_api_get_not_found(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    resp = await client.get("/api/blueprints/nope")
    assert resp.status == 404


async def test_api_validate_valid(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/blueprints/validate",
        json={
            "blueprint": {
                "name": "test",
                "steps": [{"action": "get_main_profile", "out": "p"}],
            },
        },
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["valid"] is True


async def test_api_validate_invalid(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/blueprints/validate",
        json={
            "blueprint": {
                "name": "test",
                "steps": [{"action": "open_terminal", "cmd": "$missing"}],
            },
        },
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["valid"] is False
    assert len(data["errors"]) > 0


async def test_api_validate_with_context(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/blueprints/validate",
        json={
            "blueprint": {
                "name": "test",
                "parameters": [{"name": "branch", "provenance": "user", "type": "string"}],
                "steps": [{"action": "open_terminal", "cmd": "cd $branch"}],
            },
            "context": {"branch": "main"},
        },
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["valid"] is True
    assert data["parameters"]["branch"] == "main"


async def test_api_spawn_by_name(aiohttp_client, tmp_path, monkeypatch):
    """POST /api/blueprints/spawn with a stored blueprint name."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    # Store a blueprint first
    bp = {
        "name": "spawn-test",
        "steps": [{"action": "open_terminal", "cmd": "echo test", "out": "t"}],
    }
    resp = await client.post("/api/blueprints", json=bp)
    assert resp.status == 201

    # Spawn it
    resp = await client.post(
        "/api/blueprints/spawn",
        json={"name": "spawn-test", "x": 100, "y": 200},
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["type"] == "blueprint"
    assert data["blueprint_name"] == "spawn-test"
    assert "run_id" in data


async def test_api_spawn_inline(aiohttp_client, tmp_path, monkeypatch):
    """POST /api/blueprints/spawn with an inline blueprint definition."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    bp = {
        "name": "inline-test",
        "steps": [{"action": "open_terminal", "cmd": "echo hi"}],
    }
    resp = await client.post(
        "/api/blueprints/spawn",
        json={"blueprint": bp},
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["blueprint_name"] == "inline-test"


async def test_api_spawn_validation_failure(aiohttp_client, tmp_path, monkeypatch):
    """POST /api/blueprints/spawn with invalid blueprint returns 400."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    bp = {
        "name": "bad-bp",
        "steps": [{"action": "open_terminal", "cmd": "$missing"}],
    }
    resp = await client.post(
        "/api/blueprints/spawn",
        json={"blueprint": bp},
    )
    assert resp.status == 400
    data = await resp.json()
    assert "errors" in data


async def test_api_spawn_not_found(aiohttp_client, tmp_path, monkeypatch):
    """POST /api/blueprints/spawn with nonexistent name returns 404."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/blueprints/spawn",
        json={"name": "nonexistent"},
    )
    assert resp.status == 404


async def test_api_routes_registered(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)

    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/api/blueprints" in routes
    assert "/api/blueprints/{name}" in routes
    assert "/api/blueprints/validate" in routes
    assert "/api/blueprints/spawn" in routes
