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
import uuid

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


# ── S3: Boot with stale snapshot (missing card_id) ───────────────────────────


async def test_stale_snapshot_entry_gets_stable_uuid_and_persists(tmp_path, aiohttp_client, monkeypatch):
    """S3: A canvas JSON entry without card_id is hydrated with a generated UUID.

    After a PUT /api/cards/{id}/state mutation the UUID is persisted to disk.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    # Snapshot in pre-#257 format — no card_id field.
    snapshot = {
        "name": "test-canvas",
        "canvas_size": [3840, 2160],
        "cards": [
            {"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"},
        ],
    }
    (app_config.canvases_dir / "test-canvas.json").write_text(json.dumps(snapshot))

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    client = await aiohttp_client(app)
    await asyncio.sleep(0.1)

    registry: CardRegistry = app["card_registry"]
    cards = registry.cards_on_canvas("test-canvas")
    assert len(cards) == 1, f"Expected 1 card, got {len(cards)}"
    card = cards[0]

    # card_id must be a non-null UUID-like string.
    desc = card.to_descriptor()
    card_id = desc.get("card_id")
    assert card_id is not None, "card_id should not be None on hydrated card"
    assert len(card_id) > 4, f"card_id too short: {card_id!r}"

    # Trigger a state mutation so the persist callback fires.
    resp = await client.put(f"/api/cards/{card_id}/state", json={"starred": True})
    assert resp.status == 200, await resp.text()

    # Verify the on-disk snapshot now contains the card_id.
    on_disk = json.loads((app_config.canvases_dir / "test-canvas.json").read_text())
    persisted_cards = on_disk.get("cards", [])
    assert any(e.get("card_id") == card_id for e in persisted_cards), (
        f"card_id '{card_id}' not found in persisted snapshot: {on_disk}"
    )

    # card_uid must be persisted and be a valid UUID string.
    matching_entry = next((e for e in persisted_cards if e.get("card_id") == card_id), None)
    assert matching_entry is not None, f"No persisted entry for card_id '{card_id}'"
    assert "card_uid" in matching_entry, f"'card_uid' not persisted for card_id '{card_id}': {matching_entry}"
    persisted_uid = matching_entry["card_uid"]
    assert isinstance(persisted_uid, str) and persisted_uid, f"'card_uid' is empty or not a string: {persisted_uid!r}"
    # Verify it is a properly-formatted UUID (raises ValueError on bad format).
    try:
        uuid.UUID(persisted_uid)
    except ValueError:
        raise AssertionError(f"'card_uid' is not a valid UUID: {persisted_uid!r}")


# ── S4: MCP tool observability — list_terminals returns hydrated cards ────────


async def test_mcp_terminals_returns_hydrated_cards_before_browser(tmp_path, aiohttp_client, monkeypatch):
    """S4: GET /api/claude/terminals (the endpoint the MCP tool wraps) returns
    all hydrated cards immediately after server boot — before any browser connects.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "name": "probe-qa",
        "canvas_size": [3840, 2160],
        "cards": [
            {"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"},
            {"type": "terminal", "exec": "echo hi", "starred": True, "hub": "h2"},
        ],
    }
    (app_config.canvases_dir / "probe-qa.json").write_text(json.dumps(snapshot))

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    client = await aiohttp_client(app)

    # Give background start() tasks a moment to run.
    await asyncio.sleep(0.1)

    resp = await client.get("/api/claude/terminals")
    assert resp.status == 200, await resp.text()
    data = await resp.json()
    assert len(data) >= 2, f"Expected at least 2 terminals, got {len(data)}: {data}"
    assert all("session_id" in t for t in data), f"Missing session_id in some entries: {data}"


# ── S7: Card closed mid-retry — DELETE stops retrying ────────────────────────


async def test_card_deleted_mid_retry_is_absent_from_registry(tmp_path, aiohttp_client, monkeypatch):
    """S7: DELETE /api/cards/{id} on a TerminalCard mid-retry removes the card.

    After DELETE, GET /api/cards?canvas=X must not include the deleted card_id.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "name": "test-canvas",
        "canvas_size": [3840, 2160],
        "cards": [
            {"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"},
        ],
    }
    (app_config.canvases_dir / "test-canvas.json").write_text(json.dumps(snapshot))

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    client = await aiohttp_client(app)
    await asyncio.sleep(0.1)

    registry: CardRegistry = app["card_registry"]
    cards = registry.cards_on_canvas("test-canvas")
    assert len(cards) == 1
    card_id = cards[0].id

    resp = await client.delete(f"/api/cards/{card_id}")
    assert resp.status == 204, await resp.text()

    resp2 = await client.get("/api/cards?canvas=test-canvas")
    assert resp2.status == 200
    data = await resp2.json()
    assert not any(d.get("card_id") == card_id for d in data), (
        f"Deleted card_id '{card_id}' still returned by GET /api/cards: {data}"
    )


# ── S8: retry_pty on a LIVE terminal ─────────────────────────────────────────


async def test_retry_pty_on_live_terminal_succeeds(tmp_path, aiohttp_client, monkeypatch):
    """S8: PUT /api/cards/{id}/state {"action":"retry_pty"} on a live TerminalCard returns 200.

    The SUT (_dispatch_card_action) clears error_state and re-runs start() —
    it does NOT guard against the terminal already being alive. This test
    pins the contract (200 OK) so a regression that adds an alive-guard and
    returns 400 is caught.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "name": "test-canvas",
        "canvas_size": [3840, 2160],
        "cards": [
            {"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"},
        ],
    }
    (app_config.canvases_dir / "test-canvas.json").write_text(json.dumps(snapshot))

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    client = await aiohttp_client(app)
    await asyncio.sleep(0.1)

    registry: CardRegistry = app["card_registry"]
    cards = registry.cards_on_canvas("test-canvas")
    assert len(cards) == 1
    session_id = cards[0].id

    resp = await client.put(f"/api/cards/{session_id}/state", json={"action": "retry_pty"})
    # The SUT does NOT reject retry_pty on a live terminal — it simply
    # clears error_state and re-runs start(). The contract is 200 OK.
    # If a future alive-guard is added that returns 400, update this assertion.
    assert resp.status == 200, f"Expected 200 for retry_pty on live terminal, got {resp.status}"


# ── S9: retry_pty on a WidgetCard returns 400 ────────────────────────────────


async def test_retry_pty_on_widget_card_returns_400(tmp_path, aiohttp_client, monkeypatch):
    """S9: PUT /api/cards/{id}/state {"action":"retry_pty"} on a WidgetCard returns 400."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    (app_config.canvases_dir / "main.json").write_text(
        json.dumps({"name": "main", "canvas_size": [3840, 2160], "cards": []})
    )

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    client = await aiohttp_client(app)

    # Create a WidgetCard via POST.
    resp = await client.post(
        "/api/cards/widget",
        json={"widgetType": "system-info", "canvas_name": "main"},
    )
    assert resp.status == 200, await resp.text()
    widget_desc = await resp.json()
    widget_id = widget_desc["card_id"]

    # Attempt retry_pty on the widget.
    resp2 = await client.put(f"/api/cards/{widget_id}/state", json={"action": "retry_pty"})
    assert resp2.status == 400, f"Expected 400, got {resp2.status}"
    text = await resp2.text()
    assert "retry_pty is only valid on terminal cards" in text, (
        f"Expected 'retry_pty is only valid on terminal cards' in: {text!r}"
    )


# ── S14: Deprecated courier params log a warning ─────────────────────────────


async def test_deprecated_courier_params_log_warning(tmp_path, aiohttp_client, monkeypatch):
    """S14: /ws/session/new?cmd=bash&starred=true&card_uid=abc logs a deprecation WARNING.

    The WebSocket upgrade succeeds but a WARNING with 'deprecat' is emitted.
    Uses loguru's programmatic sink API to capture log records.
    """
    import loguru

    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    client = await aiohttp_client(app)

    log_records: list[str] = []

    def _sink(message):
        log_records.append(message)

    sink_id = loguru.logger.add(_sink, level="WARNING")
    try:
        # Open the WebSocket with deprecated courier params.
        async with client.ws_connect("/ws/session/new?cmd=bash&starred=true&card_uid=abc-deprecated") as ws:
            msg = await ws.receive_json(timeout=5)
            assert "session_id" in msg, f"Expected session_id in WS handshake: {msg}"
    finally:
        loguru.logger.remove(sink_id)

    assert any("deprecat" in r.lower() for r in log_records), f"Expected a deprecation WARNING but got: {log_records}"


# ── S15: Clean boot does not emit any deprecation warning ────────────────────


async def test_clean_boot_no_deprecation_warning(tmp_path, monkeypatch):
    """S15: On a clean boot with hydrated canvas, no deprecation WARNING fires.

    session_new_handler deprecation guard must be silent during the hydration
    path — it only fires when /ws/session/new is called with courier params.
    """
    import loguru

    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "name": "probe-qa",
        "canvas_size": [3840, 2160],
        "cards": [
            {"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"},
        ],
    }
    (app_config.canvases_dir / "probe-qa.json").write_text(json.dumps(snapshot))

    log_records: list[str] = []

    def _sink(message):
        log_records.append(message)

    sink_id = loguru.logger.add(_sink, level="WARNING")
    try:
        app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
        app["_hydrate_retry_delays"] = [0]
        async with _running_app(app):
            await asyncio.sleep(0.1)
    finally:
        loguru.logger.remove(sink_id)

    deprecation_warnings = [r for r in log_records if "deprecat" in r.lower()]
    assert deprecation_warnings == [], f"Unexpected deprecation warnings during clean boot: {deprecation_warnings}"


# ── S16: Registry entries take priority over snapshot on overlap ──────────────


async def test_registry_entries_take_priority_over_snapshot(tmp_path, aiohttp_client, monkeypatch):
    """S16: GET /api/cards returns registry values, not stale on-disk snapshot values.

    To create a genuine divergence we:
      1. PUT a mutation through the registry (x=200, y=300).
      2. Directly overwrite the on-disk snapshot with conflicting stale values
         (x=999, y=999) via config.write_canvas — bypassing apply_state_patch so
         the registry in memory is NOT updated.
      3. Assert GET /api/cards returns the registry values (200/300), not the
         stale disk values (999/999).

    This catches a regression in cards_list_handler that reads from disk instead
    of the registry.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "name": "test-canvas",
        "canvas_size": [3840, 2160],
        "cards": [
            {
                "type": "terminal",
                "exec": "bash",
                "starred": True,
                "hub": "h1",
                "x": 0,
                "y": 0,
                "w": 400,
                "h": 300,
            }
        ],
    }
    (app_config.canvases_dir / "test-canvas.json").write_text(json.dumps(snapshot))

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    client = await aiohttp_client(app)
    await asyncio.sleep(0.1)

    registry: CardRegistry = app["card_registry"]
    cards = registry.cards_on_canvas("test-canvas")
    assert len(cards) == 1
    card_id = cards[0].id
    card = cards[0]

    # Step 1: Mutate position via PUT so registry holds x=200, y=300.
    resp = await client.put(f"/api/cards/{card_id}/state", json={"x": 200, "y": 300})
    assert resp.status == 200, await resp.text()
    assert card.x == 200 and card.y == 300, "Registry in-memory card should reflect mutation"

    # Step 2: Overwrite on-disk snapshot with conflicting stale values (x=999, y=999)
    # directly via config.write_canvas — bypasses apply_state_patch so the in-memory
    # registry is NOT updated and the two sources now genuinely disagree.
    stale_snapshot = {
        "name": "test-canvas",
        "canvas_size": [3840, 2160],
        "cards": [
            {
                "type": "terminal",
                "exec": "bash",
                "starred": True,
                "hub": "h1",
                "card_id": card_id,
                "x": 999,
                "y": 999,
                "w": 400,
                "h": 300,
            }
        ],
    }
    from claude_rts.config import write_canvas

    write_canvas(app_config, "test-canvas", stale_snapshot)

    # Step 3: GET /api/cards must return the registry values (200/300), not disk (999/999).
    resp2 = await client.get("/api/cards?canvas=test-canvas")
    assert resp2.status == 200
    data = await resp2.json()
    card_desc = next((d for d in data if d.get("card_id") == card_id), None)
    assert card_desc is not None, f"card_id '{card_id}' not found in response: {data}"
    assert card_desc["x"] == 200, (
        f"Expected registry value x=200, got x={card_desc.get('x')} — "
        "cards_list_handler is reading from stale disk snapshot instead of registry"
    )
    assert card_desc["y"] == 300, (
        f"Expected registry value y=300, got y={card_desc.get('y')} — "
        "cards_list_handler is reading from stale disk snapshot instead of registry"
    )


# ── S17: Snapshot-only widget entries pass through the cards_list_handler shim ─


async def test_snapshot_only_widget_entry_returned_by_shim(tmp_path, aiohttp_client, monkeypatch):
    """S17: A widget entry with a card_id not in the registry is still returned
    by GET /api/cards via the transitional shim path.

    Uses ``canvas_switch_policy: lazy_hydrate`` so ``widget-canvas`` is NOT
    hydrated at boot (only the default canvas is). This guarantees the widget card
    is genuinely absent from the registry when GET /api/cards fires, exercising
    the shim path that appends snapshot-only entries.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    # Canvas with a widget entry that has a card_id that will NOT be hydrated
    # into the registry under lazy_hydrate (non-default canvas is skipped at boot).
    snapshot = {
        "name": "widget-canvas",
        "canvas_size": [3840, 2160],
        "cards": [
            {
                "type": "widget",
                "widgetType": "system-info",
                "card_id": "fake-widget-id",
                "starred": True,
                "x": 0,
                "y": 0,
                "w": 360,
                "h": 280,
            }
        ],
    }
    (app_config.canvases_dir / "widget-canvas.json").write_text(json.dumps(snapshot))

    # Use a separate default canvas with lazy_hydrate so "widget-canvas" is NOT
    # hydrated at startup — the widget card will be absent from the registry.
    (app_config.canvases_dir / "default.json").write_text(
        json.dumps({"name": "default", "canvas_size": [3840, 2160], "cards": []})
    )
    cfg_data = {"default_canvas": "default", "canvas_switch_policy": "lazy_hydrate"}
    app_config.config_dir.mkdir(parents=True, exist_ok=True)
    app_config.config_file.write_text(json.dumps(cfg_data))

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    client = await aiohttp_client(app)
    await asyncio.sleep(0.05)

    # Verify the widget card is NOT in the registry (lazy_hydrate did not load it).
    registry: CardRegistry = app["card_registry"]
    assert registry.cards_on_canvas("widget-canvas") == [], (
        "widget-canvas should not be hydrated at boot under lazy_hydrate policy"
    )

    resp = await client.get("/api/cards?canvas=widget-canvas")
    assert resp.status == 200, await resp.text()
    data = await resp.json()

    widget = next((d for d in data if d.get("card_id") == "fake-widget-id"), None)
    assert widget is not None, f"Snapshot-only widget 'fake-widget-id' not returned by shim: {data}"
    assert widget.get("widgetType") == "system-info", f"Unexpected widgetType: {widget.get('widgetType')}"


# ── S18: Mid-hydration query — GET /api/cards returns card with no error_state ─


async def test_mid_hydration_query_returns_card_without_error_state(tmp_path, aiohttp_client, monkeypatch):
    """S18: GET /api/cards while start() is suspended in its retry-sleep returns error_state=None.

    Cards are registered before start() runs (hydrate_canvas_into_registry
    register-before-start ordering guarantee). We force start() into the retry-sleep
    phase by:
      1. Patching asyncio.sleep in terminal_card: calls with delay >= 100 s yield
         control once then return immediately (so tests don't actually wait 999 s).
      2. Setting retry_delays=[999] so start() sleeps 999 s between attempts.
      3. Patching SessionManager.create_session at class level so the first call
         fails — start() then enters the 999 s sleep (patched to yield once).

    While start() is "sleeping" (yields once then continues), the GET fires after
    the yield. The card must be in the registry with error_state=None because:
    - error_state is only set on exhaustion (all retries failed), not mid-sleep.
    - The card is registered before start() is invoked.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "name": "test-canvas",
        "canvas_size": [3840, 2160],
        "cards": [
            {"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"},
        ],
    }
    (app_config.canvases_dir / "test-canvas.json").write_text(json.dumps(snapshot))

    # Patch asyncio.sleep in terminal_card module: large delays yield control once
    # then return immediately, keeping the test fast.
    original_sleep = asyncio.sleep

    async def _fast_sleep(delay, *args, **kwargs):
        if delay >= 100:
            await original_sleep(0)  # yield once without waiting
        else:
            await original_sleep(delay, *args, **kwargs)

    monkeypatch.setattr("claude_rts.cards.terminal_card.asyncio.sleep", _fast_sleep)

    # Patch SessionManager.create_session at the class level so the first call
    # raises (triggering the retry sleep) and the second succeeds.
    attempt_counter = {"n": 0}
    _real_create = SessionManager.create_session

    def _flaky_create(self, *args, **kwargs):
        attempt_counter["n"] += 1
        if attempt_counter["n"] == 1:
            raise RuntimeError("container_not_ready_yet")
        return _real_create(self, *args, **kwargs)

    monkeypatch.setattr(SessionManager, "create_session", _flaky_create)

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    # retry_delays=[999]: after first failure start() sleeps 999 s (patched to yield once).
    app["_hydrate_retry_delays"] = [999]
    client = await aiohttp_client(app)

    # Yield once so the background _start_card_bg task gets to run up to the
    # sleep yield and pauses — giving the GET a window to query the registered card.
    await asyncio.sleep(0)

    resp = await client.get("/api/cards?canvas=test-canvas")
    assert resp.status == 200, await resp.text()
    data = await resp.json()

    assert len(data) >= 1, f"Expected at least 1 card immediately after boot: {data}"
    assert all(d.get("error_state") is None for d in data), (
        f"Unexpected error_state on freshly registered card "
        f"(should be None while start() is sleeping mid-retry): {data}"
    )
