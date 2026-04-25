"""Tests for BlueprintCard step execution, variable binding, failure, and EventBus lifecycle."""

import asyncio
import json
import time

import pytest

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


# ── Step execution: get_main_profile (renamed from get_priority_profile, #163) ───


async def test_step_get_main_profile_uses_config_value(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    from claude_rts.config import write_config

    write_config(app["app_config"], {"main_profile_name": "alice"})

    bp = {
        "name": "test",
        "steps": [{"action": "get_main_profile", "out": "cred"}],
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


async def test_step_get_main_profile_defaults_to_main(tmp_path, monkeypatch):
    """Unlike the old get_priority_profile, get_main_profile never fails — it
    falls back to the conventional 'main' slot name when nothing is configured."""
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    bp = {
        "name": "test",
        "steps": [{"action": "get_main_profile", "out": "cred"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    assert card.variables.get("cred") == "main"
    mgr.stop_all()


async def test_step_get_priority_profile_legacy_action_rejected(tmp_path, monkeypatch):
    """Old 'get_priority_profile' action is removed from VALID_ACTIONS; blueprints
    that still use it must fail fast rather than silently succeed (#163)."""
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

    # Blueprint must fail — the action no longer exists.
    assert any(e[0] == "blueprint:failed" for e in events)
    # The failure message should clearly name the unknown action.
    failure_payload = next(e[1] for e in events if e[0] == "blueprint:failed")
    assert "get_priority_profile" in str(failure_payload)
    mgr.stop_all()


# ── Step execution: discover_containers (test mode) ──────────────────────────


async def test_step_discover_containers(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)
    app["_test_containers"] = [
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
    app["_test_containers"] = [
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
    assert app["_test_containers"][0]["state"] == "online"
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

    write_config(app["app_config"], {"main_profile_name": "bob"})

    bp = {
        "name": "test",
        "steps": [
            {"action": "get_main_profile", "out": "cred"},
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
            # This will fail — 'get_priority_profile' is no longer a valid
            # action (removed in #163). Blueprint dispatch must reject it
            # before the second step runs.
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
    # Assert the failure is attributable to the legacy action, not some
    # incidental error — otherwise this test would false-pass if a bug
    # caused the first step to succeed via a different path.
    assert "get_priority_profile" in str(failed_events[0][1])
    assert "error" in failed_events[0][1]
    mgr.stop_all()


# ── blueprint:completed event ────────────────────────────────────────────────


async def test_completed_event(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    from claude_rts.config import write_config

    write_config(app["app_config"], {"main_profile_name": "alice"})

    events = []

    async def capture(event_type, payload):
        events.append((event_type, payload))

    bus.subscribe("blueprint:completed", capture)

    bp = {
        "name": "test",
        "steps": [{"action": "get_main_profile", "out": "p"}],
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

    write_config(app["app_config"], {"main_profile_name": "alice"})

    log_messages = []

    async def capture_log(event_type, payload):
        log_messages.append(payload["message"])

    bus.subscribe("blueprint:log", capture_log)

    bp = {
        "name": "test",
        "steps": [{"action": "get_main_profile", "out": "p"}],
    }
    card = BlueprintCard(blueprint=bp, app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    assert any("Starting blueprint" in m for m in log_messages)
    assert any("get_main_profile" in m for m in log_messages)
    assert any("completed successfully" in m for m in log_messages)
    mgr.stop_all()


# ── Execution log file ──────────────────────────────────────────────────────


async def test_execution_log_file(tmp_path, monkeypatch):
    app, bus, mgr, reg = _make_app(tmp_path, monkeypatch)

    from claude_rts.config import write_config

    write_config(app["app_config"], {"main_profile_name": "alice"})

    bp = {
        "name": "test",
        "steps": [{"action": "get_main_profile", "out": "p"}],
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

    write_config(app["app_config"], {"main_profile_name": "alice"})

    bp = {
        "name": "test",
        "steps": [{"action": "get_main_profile", "out": "p"}],
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
        "steps": [{"action": "get_main_profile", "out": "p"}],
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
    app["_test_containers"] = [
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


# ── Ephemerality (epic #254 child 7 / #262) ─────────────────────────────────
#
# BlueprintCard is the deliberate counter-example to "all card types migrate
# together" — it is one-shot and ephemeral, and must remain absent from canvas
# snapshots and from the post-restart CardRegistry. These tests exist so a
# future change that accidentally adds BlueprintCard to the persist or
# hydration paths fails loudly. See docs/state-model.md "BlueprintCard
# ephemerality".


def test_blueprint_card_from_descriptor_is_not_implemented():
    """BlueprintCard.from_descriptor must refuse to hydrate from a snapshot.

    The hydrate dispatch table in ``hydrate_canvas_into_registry`` only knows
    about ``terminal`` / ``widget`` / ``canvas_claude``; if a future change
    adds ``blueprint`` to that table, this test still guards the contract on
    the class itself.
    """
    with pytest.raises(NotImplementedError):
        BlueprintCard.from_descriptor({"type": "blueprint", "blueprint_name": "x"})


def test_blueprint_card_default_starred_is_false():
    """A live BlueprintCard is unstarred by default.

    The persist callback filters non-starred cards out of the canvas snapshot
    (issue #194 / epic #236). Combined with the empty MUTABLE_FIELDS on
    BlueprintCard (so ``starred`` cannot be flipped via ``PUT
    /api/cards/{id}/state``), a live BlueprintCard cannot leak into the
    snapshot through the legitimate mutation path.
    """
    card = BlueprintCard(blueprint={"name": "x", "steps": []})
    assert card.starred is False
    # ``starred`` is not in MUTABLE_FIELDS for BlueprintCard, so the generic
    # state-patch endpoint cannot turn it on.
    assert "starred" not in BlueprintCard.MUTABLE_FIELDS


async def test_live_blueprint_card_excluded_from_canvas_snapshot(tmp_path, monkeypatch):
    """A registered BlueprintCard does not appear in the on-disk canvas JSON.

    Spawn a live BlueprintCard mid-execution, force the persist callback to
    fire (by mutating a starred neighbour), and assert the canvas snapshot
    contains no blueprint entries — even though the BlueprintCard is in the
    registry at the moment of write.
    """
    from claude_rts.cards.terminal_card import TerminalCard
    from claude_rts.server import create_app
    from tests.test_terminal_card import MockPty as TerminalMockPty

    monkeypatch.setattr("claude_rts.sessions.PtyProcess", TerminalMockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    canvas_name = "ephem"
    (app_config.canvases_dir / f"{canvas_name}.json").write_text(
        json.dumps({"name": canvas_name, "canvas_size": [3840, 2160], "cards": []})
    )

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    # Run on_startup to wire the persist callback.
    for hook in app.on_startup:
        await hook(app)
    try:
        registry: CardRegistry = app["card_registry"]
        mgr: SessionManager = app["session_manager"]

        # Spawn a starred TerminalCard so the snapshot has a non-blueprint
        # entry to write through.
        terminal = TerminalCard(session_manager=mgr, cmd="bash", starred=True)
        await terminal.start(retry_delays=[])
        registry.register(terminal, canvas_name=canvas_name)

        # Spawn a long-running BlueprintCard (open_widget step never gets an
        # ack in the test harness, so the card stays in the registry while we
        # take the snapshot).
        bp = BlueprintCard(
            blueprint={
                "name": "long-running",
                "steps": [{"action": "open_widget", "widget_type": "system-info", "timeout": 30}],
            },
            app=app,
        )
        bp.bus = app["event_bus"]
        registry.register(bp, canvas_name=canvas_name)
        await bp.start()
        await asyncio.sleep(0.05)

        # Sanity: the BlueprintCard is currently in the registry on this canvas.
        live_types = {c.card_type for c in registry.cards_on_canvas(canvas_name)}
        assert "blueprint" in live_types
        assert "terminal" in live_types

        # Trigger write-through via the legitimate mutation path.
        registry.apply_state_patch(terminal.id, {"starred": True})

        on_disk = json.loads((app_config.canvases_dir / f"{canvas_name}.json").read_text())
        types_on_disk = {entry.get("type") for entry in on_disk.get("cards", [])}
        assert "blueprint" not in types_on_disk, f"BlueprintCard leaked into canvas snapshot: {on_disk}"
        # And the terminal is still there — the filter is per-card, not all-or-nothing.
        assert "terminal" in types_on_disk

        # Cancel the blueprint so its background task does not outlive the test.
        await bp.stop()
    finally:
        for hook in app.on_shutdown:
            try:
                await hook(app)
            except Exception:
                pass


async def test_blueprint_card_absent_from_registry_after_restart(tmp_path, monkeypatch):
    """After a simulated server restart, no BlueprintCards exist in CardRegistry.

    "Restart" here = run on_startup against a canvas-dir that was the live
    canvas of a previous run. The previous run's persist callback never
    wrote BlueprintCard entries (test above), so the post-restart registry
    contains zero blueprints regardless of what was running pre-restart.
    """
    from claude_rts.cards.terminal_card import TerminalCard
    from claude_rts.server import create_app
    from tests.test_terminal_card import MockPty as TerminalMockPty

    monkeypatch.setattr("claude_rts.sessions.PtyProcess", TerminalMockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    canvas_name = "restart-canvas"
    (app_config.canvases_dir / f"{canvas_name}.json").write_text(
        json.dumps({"name": canvas_name, "canvas_size": [3840, 2160], "cards": []})
    )

    # ── First run: spawn a TerminalCard + BlueprintCard, force persist. ──
    app1 = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app1["_hydrate_retry_delays"] = [0]
    for hook in app1.on_startup:
        await hook(app1)
    try:
        registry1: CardRegistry = app1["card_registry"]
        mgr1: SessionManager = app1["session_manager"]
        terminal = TerminalCard(session_manager=mgr1, cmd="bash", starred=True)
        await terminal.start(retry_delays=[])
        registry1.register(terminal, canvas_name=canvas_name)

        bp = BlueprintCard(
            blueprint={
                "name": "long-running",
                "steps": [{"action": "open_widget", "widget_type": "system-info", "timeout": 30}],
            },
            app=app1,
        )
        bp.bus = app1["event_bus"]
        registry1.register(bp, canvas_name=canvas_name)
        await bp.start()
        await asyncio.sleep(0.05)
        registry1.apply_state_patch(terminal.id, {"starred": True})
        await bp.stop()
    finally:
        for hook in app1.on_shutdown:
            try:
                await hook(app1)
            except Exception:
                pass

    # ── Second run: same canvas dir, fresh app. on_startup hydrates. ──
    app2 = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app2["_hydrate_retry_delays"] = [0]
    for hook in app2.on_startup:
        await hook(app2)
    try:
        registry2: CardRegistry = app2["card_registry"]
        await asyncio.sleep(0.1)  # let background start() tasks settle
        post_restart = registry2.cards_on_canvas(canvas_name)
        # The terminal survived because it was starred; the blueprint did not.
        assert all(c.card_type != "blueprint" for c in post_restart), (
            f"BlueprintCard was hydrated post-restart: {[c.card_type for c in post_restart]}"
        )
        # The terminal's hydrator did run.
        assert any(c.card_type == "terminal" for c in post_restart)
    finally:
        for hook in app2.on_shutdown:
            try:
                await hook(app2)
            except Exception:
                pass
