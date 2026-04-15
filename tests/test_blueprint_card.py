"""Tests for BlueprintCard step execution, variable binding, failure, and EventBus lifecycle."""

import asyncio
import time


from claude_rts.cards.blueprint_card import BlueprintCard
from claude_rts.cards.card_registry import CardRegistry
from claude_rts.event_bus import EventBus
from claude_rts.sessions import SessionManager
from claude_rts import config


class MockPty:
    def __init__(self):
        self._alive = True
        self._written = []

    def isalive(self):
        return self._alive

    def read(self):
        time.sleep(0.1)
        if not self._alive:
            raise EOFError()
        return ""

    def write(self, text):
        self._written.append(text)

    def setwinsize(self, rows, cols):
        pass

    def terminate(self, force=False):
        self._alive = False

    @classmethod
    def spawn(cls, cmd, dimensions=(24, 80)):
        return cls()


def _make_app(tmp_path, monkeypatch):
    """Create a minimal app dict for BlueprintCard testing."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    bus = EventBus()
    mgr = SessionManager()
    card_registry = CardRegistry(bus=bus)

    app = {
        "app_config": app_config,
        "event_bus": bus,
        "session_manager": mgr,
        "card_registry": card_registry,
    }
    return app, bus, mgr, card_registry


# ── Basic lifecycle ──────────────────────────────────────────────────────────


async def test_blueprint_card_type():
    card = BlueprintCard(blueprint={"name": "test", "steps": []})
    assert card.card_type == "blueprint"
    assert card.hidden is False


async def test_blueprint_card_descriptor():
    card = BlueprintCard(blueprint={"name": "my-bp", "steps": []})
    card.layout = {"x": 100, "y": 200}
    desc = card.to_descriptor()
    assert desc["type"] == "blueprint"
    assert desc["blueprint_name"] == "my-bp"
    assert desc["run_id"] == card.run_id
    assert desc["x"] == 100


# ── Step execution: get_priority_profile ─────────────────────────────────────


async def test_step_get_priority_profile(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    # Set up priority profile in config
    from claude_rts.config import write_config

    write_config(app["app_config"], {"priority_profile": "alice"})

    bp = {
        "name": "test",
        "steps": [{"action": "get_priority_profile", "out": "cred"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    # Wait for execution to complete
    await asyncio.sleep(0.5)

    assert card.variables.get("cred") == "alice"
    # Card should have self-unregistered
    assert reg.get(card.id) is None
    mgr.stop_all()


async def test_step_get_priority_profile_missing(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    events = []

    async def capture_events(event_type, payload):
        events.append((event_type, payload))

    bus.subscribe("blueprint:failed", capture_events)

    bp = {
        "name": "test",
        "steps": [{"action": "get_priority_profile", "out": "cred"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    # Should have emitted blueprint:failed
    assert any(e[0] == "blueprint:failed" for e in events)
    mgr.stop_all()


# ── Step execution: discover_containers (test mode) ──────────────────────────


async def test_step_discover_containers(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)
    app["_test_vm_containers"] = [
        {"name": "hub1", "state": "online"},
        {"name": "hub2", "state": "offline"},
    ]

    bp = {
        "name": "test",
        "steps": [{"action": "discover_containers", "out": "containers"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    assert card.variables.get("containers") == ["hub1", "hub2"]
    mgr.stop_all()


# ── Step execution: start_container (test mode) ─────────────────────────────


async def test_step_start_container(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)
    app["_test_vm_containers"] = [
        {"name": "hub1", "state": "offline"},
    ]

    bp = {
        "name": "test",
        "steps": [{"action": "start_container", "container": "hub1", "out": "c"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(1.0)

    assert card.variables.get("c") == "hub1"
    # Container should have been flipped to online
    assert app["_test_vm_containers"][0]["state"] == "online"
    mgr.stop_all()


# ── Step execution: open_terminal ────────────────────────────────────────────


async def test_step_open_terminal(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    bp = {
        "name": "test",
        "steps": [{"action": "open_terminal", "cmd": "echo hello", "out": "term"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(1.2)  # step sleeps 0.5s for shell settle + processing

    term_desc = card.variables.get("term")
    assert term_desc is not None
    assert term_desc["type"] == "terminal"

    # Terminal card should be in the registry
    terminals = reg.list_terminals()
    assert len(terminals) == 1

    # Clean up
    for t in terminals:
        await t.stop()
    mgr.stop_all()


# ── Variable binding chain ──────────────────────────────────────────────────


async def test_variable_binding_chain(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    from claude_rts.config import write_config

    write_config(app["app_config"], {"priority_profile": "bob"})

    bp = {
        "name": "test",
        "steps": [
            {"action": "get_priority_profile", "out": "cred"},
            {"action": "open_terminal", "cmd": "echo $cred", "out": "term"},
        ],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(1.2)  # step sleeps 0.5s for shell settle + processing

    assert card.variables["cred"] == "bob"
    term_desc = card.variables["term"]
    # The terminal was opened with the interpolated command
    assert term_desc["type"] == "terminal"

    # Clean up
    for t in reg.list_terminals():
        await t.stop()
    mgr.stop_all()


# ── Failure halts execution ─────────────────────────────────────────────────


async def test_failure_halts_execution(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    events = []

    async def capture(event_type, payload):
        events.append((event_type, payload))

    bus.subscribe("blueprint:failed", capture)

    bp = {
        "name": "test",
        "steps": [
            # This will fail because no priority_profile
            {"action": "get_priority_profile", "out": "cred"},
            # This should NOT execute
            {"action": "open_terminal", "cmd": "echo should-not-run", "out": "term"},
        ],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    # Second step should not have run
    assert "term" not in card.variables
    # Should have emitted failed event
    failed_events = [e for e in events if e[0] == "blueprint:failed"]
    assert len(failed_events) == 1
    assert "error" in failed_events[0][1]
    mgr.stop_all()


# ── blueprint:completed event ────────────────────────────────────────────────


async def test_completed_event(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    from claude_rts.config import write_config

    write_config(app["app_config"], {"priority_profile": "alice"})

    events = []

    async def capture(event_type, payload):
        events.append((event_type, payload))

    bus.subscribe("blueprint:completed", capture)

    bp = {
        "name": "test",
        "steps": [{"action": "get_priority_profile", "out": "p"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    completed = [e for e in events if e[0] == "blueprint:completed"]
    assert len(completed) == 1
    assert completed[0][1]["blueprint_name"] == "test"
    mgr.stop_all()


# ── blueprint:log events ────────────────────────────────────────────────────


async def test_log_events(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    from claude_rts.config import write_config

    write_config(app["app_config"], {"priority_profile": "alice"})

    log_messages = []

    async def capture_log(event_type, payload):
        log_messages.append(payload["message"])

    bus.subscribe("blueprint:log", capture_log)

    bp = {
        "name": "test",
        "steps": [{"action": "get_priority_profile", "out": "p"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    assert any("Starting blueprint" in m for m in log_messages)
    assert any("get_priority_profile" in m for m in log_messages)
    assert any("completed successfully" in m for m in log_messages)
    mgr.stop_all()


# ── Execution log file ──────────────────────────────────────────────────────


async def test_execution_log_file(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    from claude_rts.config import write_config

    write_config(app["app_config"], {"priority_profile": "alice"})

    bp = {
        "name": "test",
        "steps": [{"action": "get_priority_profile", "out": "p"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    # Check that the log file was created
    log_dir = app["app_config"].config_dir / "blueprint_logs"
    log_files = list(log_dir.glob("*.log"))
    assert len(log_files) == 1

    content = log_files[0].read_text(encoding="utf-8")
    assert "Starting blueprint" in content
    assert "completed successfully" in content
    mgr.stop_all()


# ── Self-close on completion ────────────────────────────────────────────────


async def test_self_close_on_completion(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    from claude_rts.config import write_config

    write_config(app["app_config"], {"priority_profile": "alice"})

    bp = {
        "name": "test",
        "steps": [{"action": "get_priority_profile", "out": "p"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    assert reg.get(card.id) is not None

    await card.start()
    await asyncio.sleep(0.5)

    # Card should have self-unregistered after completion
    assert reg.get(card.id) is None
    mgr.stop_all()


async def test_self_close_on_failure(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    bp = {
        "name": "test",
        "steps": [{"action": "get_priority_profile", "out": "p"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    # Card should have self-unregistered after failure
    assert reg.get(card.id) is None
    mgr.stop_all()


# ── Parameters with defaults ────────────────────────────────────────────────


async def test_parameters_with_defaults(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    bp = {
        "name": "test",
        "parameters": [
            {"name": "msg", "provenance": "static", "type": "string", "default": "hello"},
        ],
        "steps": [{"action": "open_terminal", "cmd": "echo $msg", "out": "t"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    # The terminal should have been created with the default value
    assert card.variables["msg"] == "hello"
    for t in reg.list_terminals():
        await t.stop()
    mgr.stop_all()


# ── Parameters from context ─────────────────────────────────────────────────


async def test_parameters_from_context(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    bp = {
        "name": "test",
        "parameters": [
            {"name": "msg", "provenance": "user", "type": "string"},
        ],
        "steps": [{"action": "open_terminal", "cmd": "echo $msg", "out": "t"}],
    }
    card = BlueprintCard(blueprint=bp, app=app, context={"msg": "world"})
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    assert card.variables["msg"] == "world"
    for t in reg.list_terminals():
        await t.stop()
    mgr.stop_all()


# ── for_each step ───────────────────────────────────────────────────────────


async def test_for_each_step(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)
    app["_test_vm_containers"] = [
        {"name": "hub1", "state": "online"},
        {"name": "hub2", "state": "online"},
    ]

    bp = {
        "name": "test",
        "steps": [
            {"action": "discover_containers", "out": "containers"},
            {
                "action": "for_each",
                "list": "$containers",
                "item_var": "c",
                "steps": [
                    {"action": "open_terminal", "cmd": "echo $c"},
                ],
            },
        ],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(1.0)

    # Two terminals should have been opened
    terminals = reg.list_terminals()
    assert len(terminals) == 2

    for t in terminals:
        await t.stop()
    mgr.stop_all()


# ── Timeout handling ────────────────────────────────────────────────────────


async def test_step_timeout(tmp_path, monkeypatch):
    """A step that takes too long should fail with a timeout error."""
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    events = []

    async def capture(event_type, payload):
        events.append((event_type, payload))

    bus.subscribe("blueprint:failed", capture)

    # Inject a slow step by using open_widget which will never get an ack
    bp = {
        "name": "test",
        "steps": [
            {"action": "open_widget", "widget_type": "system-info", "timeout": 0.5},
        ],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(2.0)

    failed = [e for e in events if e[0] == "blueprint:failed"]
    assert len(failed) == 1
    mgr.stop_all()
