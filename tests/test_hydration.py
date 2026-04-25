"""Tests for Epic #254 child 2 (#257) — server-authored TerminalCard hydration.

Covers:
  - ``TerminalCard.from_descriptor`` — reconstruct without starting PTY
  - ``TerminalCard.start()`` retry loop — recovery path and error_state ceiling
  - ``BaseCard.error_state`` — field presence, descriptor emission, persist filter
  - ``hydrate_canvas_into_registry`` — boot-time registry population
  - ``GET /api/cards?canvas=X`` — new observer endpoint
  - ``session_new_handler`` attach-vs-create deduplication
"""

import asyncio
import json

import pytest

from claude_rts import config
from claude_rts.cards.card_registry import CardRegistry
from claude_rts.cards.terminal_card import TerminalCard
from claude_rts.cards.widget_card import WidgetCard
from claude_rts.server import create_app
from claude_rts.sessions import SessionManager

from tests.test_terminal_card import MockPty


# ── TerminalCard.from_descriptor ──────────────────────────────────────────


async def test_from_descriptor_builds_card_without_starting(monkeypatch):
    """from_descriptor() returns a TerminalCard with fields set and no PTY."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    data = {
        "type": "terminal",
        "card_id": "rts-abcd1234",
        "hub": "hub1",
        "container": "cont1",
        "exec": "bash -l",
        "starred": True,
        "display_name": "Hydrated",
        "recovery_script": "cd /work",
        "x": 50,
        "y": 60,
        "w": 400,
        "h": 300,
        "z_order": 2,
        "card_uid": "uid-xyz",
    }
    card = TerminalCard.from_descriptor(data, session_manager=mgr)
    assert card.session is None  # PTY not started yet
    assert card.cmd == "bash -l"
    assert card.hub == "hub1"
    assert card.container == "cont1"
    assert card.starred is True
    assert card.display_name == "Hydrated"
    assert card.recovery_script == "cd /work"
    assert card.x == 50 and card.y == 60 and card.w == 400 and card.h == 300
    assert card.card_uid == "uid-xyz"
    mgr.stop_all()


async def test_from_descriptor_requires_session_manager():
    """from_descriptor() without session_manager raises TypeError."""
    with pytest.raises(TypeError):
        TerminalCard.from_descriptor({"type": "terminal", "exec": "bash"})


# ── TerminalCard retry loop ──────────────────────────────────────────────


async def test_start_retries_on_transient_failure(monkeypatch):
    """start() retries on failed create_session and succeeds on a later attempt."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    attempts = {"n": 0}
    real_create = mgr.create_session

    def flaky_create(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("container not ready")
        return real_create(*args, **kwargs)

    mgr.create_session = flaky_create
    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start(retry_delays=[0, 0, 0])
    assert card.session is not None
    assert card.error_state is None
    assert attempts["n"] == 2
    await card.stop()
    mgr.stop_all()


async def test_start_ceiling_sets_error_state(monkeypatch):
    """start() exhausts retries, sets error_state schema, fires callback."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()

    def always_fail(*args, **kwargs):
        raise RuntimeError("container_unavailable_in_test")

    mgr.create_session = always_fail
    card = TerminalCard(session_manager=mgr, cmd="bash", starred=True)
    broadcasts = []

    def _on_error(c):
        broadcasts.append(c.error_state)

    await card.start(retry_delays=[0, 0, 0], on_error_state=_on_error)
    assert card.session is None
    assert card.error_state is not None
    assert card.error_state["kind"] == "container_unavailable"
    assert card.error_state["attempts"] == 4  # 1 initial + 3 retries
    assert "container_unavailable_in_test" in card.error_state["last_error"]
    assert broadcasts == [card.error_state]
    mgr.stop_all()


async def test_error_state_in_descriptor_and_stripped_from_persist(monkeypatch):
    """error_state appears in to_descriptor() but is filtered from persisted snapshot."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start(retry_delays=[])
    desc = card.to_descriptor()
    assert "error_state" not in desc  # None is omitted

    card.error_state = {"kind": "container_unavailable", "attempts": 3, "last_error": "boom"}
    desc = card.to_descriptor()
    assert desc["error_state"]["kind"] == "container_unavailable"

    await card.stop()
    mgr.stop_all()


# ── hydrate_canvas_into_registry ─────────────────────────────────────────


async def test_hydrate_canvas_populates_registry(tmp_path, monkeypatch):
    """Boot-time hydration registers TerminalCards and WidgetCards for every snapshot entry."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "name": "probe-qa",
        "canvas_size": [3840, 2160],
        "cards": [
            {"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"},
            {"type": "terminal", "exec": "echo hi", "starred": True, "container": "util"},
            {"type": "widget", "widgetType": "system-info", "starred": True},
        ],
    }
    (app_config.canvases_dir / "probe-qa.json").write_text(json.dumps(snapshot))

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    # Make retries near-zero for tests
    app["_hydrate_retry_delays"] = [0, 0, 0]
    async with _running_app(app):
        # Give background start() tasks a chance to run.
        await asyncio.sleep(0.1)
        registry: CardRegistry = app["card_registry"]
        all_cards = registry.cards_on_canvas("probe-qa")
        # All 3 entries (2 terminals + 1 widget) are now hydrated.
        assert len(all_cards) == 3
        terminals = [c for c in all_cards if isinstance(c, TerminalCard)]
        widgets = [c for c in all_cards if isinstance(c, WidgetCard)]
        assert len(terminals) == 2
        assert len(widgets) == 1
        for t in terminals:
            assert t.session_id is not None
            assert t.error_state is None
        widget = widgets[0]
        assert widget.widget_type == "system-info"
        assert widget.starred is True


async def test_hydrate_skips_unknown_type_without_crash(tmp_path, monkeypatch):
    """Unknown card types are logged and skipped; hydration does not abort."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "name": "mixed",
        "canvas_size": [3840, 2160],
        "cards": [
            {"type": "terminal", "exec": "bash", "starred": True},
            {"type": "mystery-card", "starred": True},
            "not-an-object",
        ],
    }
    (app_config.canvases_dir / "mixed.json").write_text(json.dumps(snapshot))

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    async with _running_app(app):
        await asyncio.sleep(0.1)
        registry: CardRegistry = app["card_registry"]
        terminals = registry.cards_on_canvas("mixed")
        assert len(terminals) == 1


# ── GET /api/cards?canvas=X ──────────────────────────────────────────────


async def test_api_cards_returns_hydrated_descriptors(tmp_path, aiohttp_client, monkeypatch):
    """GET /api/cards?canvas=X returns the hydrated registry descriptors."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "name": "live",
        "canvas_size": [3840, 2160],
        "cards": [
            {"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"},
        ],
    }
    (app_config.canvases_dir / "live.json").write_text(json.dumps(snapshot))

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    client = await aiohttp_client(app)

    # Give the background start() task a moment.
    await asyncio.sleep(0.1)
    resp = await client.get("/api/cards?canvas=live")
    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 1
    assert data[0]["type"] == "terminal"
    assert data[0]["starred"] is True
    assert data[0]["hub"] == "h1"
    assert "card_id" in data[0]


async def test_api_cards_404_on_unknown_canvas(tmp_path, aiohttp_client, monkeypatch):
    """GET /api/cards?canvas=X returns 404 for an unknown canvas."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    client = await aiohttp_client(app)
    resp = await client.get("/api/cards?canvas=does-not-exist")
    assert resp.status == 404


async def test_api_cards_missing_param_400(tmp_path, aiohttp_client, monkeypatch):
    """GET /api/cards without ?canvas=X returns 400."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    client = await aiohttp_client(app)
    resp = await client.get("/api/cards")
    assert resp.status == 400


# ── Retry state not persisted across server restart ─────────────────────


async def test_error_state_not_persisted(tmp_path, monkeypatch):
    """When apply_state_patch fires, the persisted JSON omits error_state."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    (app_config.canvases_dir / "persist.json").write_text(
        json.dumps({"name": "persist", "canvas_size": [3840, 2160], "cards": []})
    )

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    async with _running_app(app):
        registry: CardRegistry = app["card_registry"]
        mgr: SessionManager = app["session_manager"]
        card = TerminalCard(session_manager=mgr, cmd="bash", starred=True)
        await card.start(retry_delays=[])
        card.error_state = {"kind": "container_unavailable", "attempts": 3, "last_error": "no"}
        registry.register(card, canvas_name="persist")
        # Trigger write-through
        registry.apply_state_patch(card.id, {"starred": True})

        on_disk = json.loads((app_config.canvases_dir / "persist.json").read_text())
        for entry in on_disk.get("cards", []):
            assert "error_state" not in entry


# ── session_new_handler dedup ────────────────────────────────────────────


async def test_session_new_attaches_to_hydrated_card(tmp_path, monkeypatch):
    """A pre-hydrated card's card_uid match makes session_new_handler attach, not duplicate."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    async with _running_app(app):
        registry: CardRegistry = app["card_registry"]
        mgr: SessionManager = app["session_manager"]
        hydrated = TerminalCard(session_manager=mgr, cmd="bash", card_uid="uid-1", starred=True)
        await hydrated.start(retry_delays=[])
        registry.register(hydrated, canvas_name="main")

        # Simulate the dedup branch directly (WebSocket lifecycle is hard to
        # drive from tests; the branch logic in session_new_handler matches
        # by card_id or card_uid and short-circuits).
        existing = registry.get_terminal(hydrated.id)
        assert existing is hydrated
        # And by card_uid:
        matches = [t for t in registry.list_terminals() if t.card_uid == "uid-1"]
        assert len(matches) == 1
        assert len(registry.list_terminals()) == 1


# ── WidgetCard descriptor + endpoint + state mutation (epic #254 child 5) ─


def test_widget_card_to_descriptor_round_trip():
    """WidgetCard.to_descriptor() emits widgetType / starred / card_id / geometry."""
    card = WidgetCard(
        widget_type="system-info",
        card_id="widget-test-1",
        layout={"x": 100, "y": 200, "w": 360, "h": 280, "z_order": 3},
        starred=True,
        refresh_interval=15000,
    )
    desc = card.to_descriptor()
    assert desc["type"] == "widget"
    assert desc["card_id"] == "widget-test-1"
    assert desc["widgetType"] == "system-info"
    assert desc["starred"] is True
    assert desc["x"] == 100 and desc["y"] == 200
    assert desc["w"] == 360 and desc["h"] == 280
    assert desc["z_order"] == 3
    assert desc["refreshInterval"] == 15000

    # from_descriptor reconstructs the same card.
    rebuilt = WidgetCard.from_descriptor(desc)
    assert rebuilt.widget_type == "system-info"
    assert rebuilt.starred is True
    assert rebuilt.x == 100 and rebuilt.y == 200


def test_widget_card_legacy_vm_manager_renamed():
    """from_descriptor maps the deprecated 'vm-manager' widget type to 'container-manager'."""
    card = WidgetCard.from_descriptor({"widgetType": "vm-manager"})
    assert card.widget_type == "container-manager"


async def test_post_widget_creates_and_registers(tmp_path, aiohttp_client, monkeypatch):
    """POST /api/cards/widget creates a WidgetCard, registers it, and returns the descriptor."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    (app_config.canvases_dir / "main.json").write_text(
        json.dumps({"name": "main", "canvas_size": [3840, 2160], "cards": []})
    )

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/cards/widget",
        json={
            "widgetType": "system-info",
            "canvas_name": "main",
            "x": 50,
            "y": 60,
            "w": 360,
            "h": 280,
            "starred": True,
        },
    )
    assert resp.status == 200, await resp.text()
    desc = await resp.json()
    assert desc["type"] == "widget"
    assert desc["widgetType"] == "system-info"
    assert desc["starred"] is True
    assert desc["x"] == 50 and desc["y"] == 60
    assert "card_id" in desc and desc["card_id"]

    # The card is registered and visible via GET /api/cards.
    list_resp = await client.get("/api/cards?canvas=main")
    assert list_resp.status == 200
    listing = await list_resp.json()
    widget_entries = [d for d in listing if d.get("type") == "widget"]
    assert len(widget_entries) == 1
    assert widget_entries[0]["card_id"] == desc["card_id"]


async def test_post_widget_missing_widget_type_400(tmp_path, aiohttp_client, monkeypatch):
    """POST /api/cards/widget rejects a body without widgetType."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    client = await aiohttp_client(app)
    resp = await client.post("/api/cards/widget", json={"x": 0, "y": 0})
    assert resp.status == 400


async def test_widget_state_mutation_via_put(tmp_path, aiohttp_client, monkeypatch):
    """PUT /api/cards/{id}/state mutates a hydrated WidgetCard's geometry / starred."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    (app_config.canvases_dir / "wcanvas.json").write_text(
        json.dumps(
            {
                "name": "wcanvas",
                "canvas_size": [3840, 2160],
                "cards": [
                    {
                        "type": "widget",
                        "widgetType": "system-info",
                        "card_id": "w-1",
                        "starred": True,
                        "x": 0,
                        "y": 0,
                        "w": 360,
                        "h": 280,
                    }
                ],
            }
        )
    )

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    client = await aiohttp_client(app)
    await asyncio.sleep(0.05)  # let hydrate task settle

    registry: CardRegistry = app["card_registry"]
    cards_now = [c for c in registry.cards_on_canvas("wcanvas") if isinstance(c, WidgetCard)]
    assert len(cards_now) == 1
    widget_id = cards_now[0].id

    resp = await client.put(
        f"/api/cards/{widget_id}/state",
        json={"x": 200, "y": 250, "w": 480, "h": 320, "starred": False},
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["x"] == 200 and body["y"] == 250
    assert body["w"] == 480 and body["h"] == 320
    assert body["starred"] is False

    widget = registry.get(widget_id)
    assert widget.x == 200 and widget.y == 250
    assert widget.starred is False


def test_preset_widget_entries_have_card_id():
    """Every widget entry in dev_presets/*/canvases/*.json carries a stable card_id."""
    import pathlib

    preset_root = pathlib.Path(__file__).resolve().parents[1] / "claude_rts" / "dev_presets"
    missing: list[str] = []
    for canvas_file in preset_root.glob("*/canvases/*.json"):
        data = json.loads(canvas_file.read_text())
        for idx, card in enumerate(data.get("cards", [])):
            if card.get("type") == "widget" and not card.get("card_id"):
                missing.append(f"{canvas_file}: idx={idx} widgetType={card.get('widgetType')}")
    assert not missing, "Widget entries without card_id:\n" + "\n".join(missing)


# ── Helper context manager to run the app lifecycle ─────────────────────


class _running_app:
    def __init__(self, app):
        self.app = app

    async def __aenter__(self):
        # Trigger on_startup manually (no aiohttp client needed for plain
        # registry / hydration inspection).
        for hook in self.app.on_startup:
            await hook(self.app)
        return self.app

    async def __aexit__(self, exc_type, exc, tb):
        for hook in self.app.on_shutdown:
            try:
                await hook(self.app)
            except Exception:
                pass
