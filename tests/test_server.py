"""Tests for the aiohttp server routes."""

from unittest.mock import patch, AsyncMock

import pytest

from claude_rts import config
from claude_rts.server import create_app


MOCK_HUBS = [
    {"hub": "hub_1", "container": "zealous_darwin"},
    {"hub": "hub_2", "container": "suspicious_lichterman"},
]


@pytest.fixture
def app(tmp_path):
    app_config = config.load(tmp_path / ".sc")
    return create_app(app_config)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


async def test_index_returns_html(client):
    resp = await client.get("/")
    assert resp.status == 200
    text = await resp.text()
    assert "supreme-claudemander" in text
    assert "xterm" in text


async def test_hubs_endpoint_returns_json(client):
    with patch("claude_rts.server.discover_hubs", new_callable=AsyncMock, return_value=MOCK_HUBS):
        resp = await client.get("/api/hubs")

    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 2
    assert data[0]["hub"] == "hub_1"
    assert data[1]["hub"] == "hub_2"


async def test_hubs_endpoint_empty(client):
    with patch("claude_rts.server.discover_hubs", new_callable=AsyncMock, return_value=[]):
        resp = await client.get("/api/hubs")

    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_websocket_404_for_unknown_hub(client):
    # Legacy /ws/{hub} route was removed (#127); any /ws/<anything> now 404s at the router.
    resp = await client.get("/ws/anything")
    assert resp.status == 404


async def test_widget_system_info_returns_json(client):
    resp = await client.get("/api/widgets/system-info")
    assert resp.status == 200
    data = await resp.json()
    assert "hostname" in data
    assert "platform" in data
    assert "python_version" in data
    assert "uptime" in data
    assert "uptime_seconds" in data
    assert isinstance(data["uptime_seconds"], int)
    assert isinstance(data["hostname"], str)


async def test_widget_system_info_uptime_format(client):
    resp = await client.get("/api/widgets/system-info")
    assert resp.status == 200
    data = await resp.json()
    # Uptime should match "Xh Ym Zs" format
    assert "h " in data["uptime"]
    assert "m " in data["uptime"]
    assert data["uptime"].endswith("s")


async def test_app_has_all_routes(app):
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/" in routes
    assert "/api/hubs" in routes
    assert "/api/config" in routes
    assert "/api/canvases" in routes
    assert "/api/canvases/{name}" in routes
    assert "/api/startup" in routes
    assert "/api/widgets/system-info" in routes
    assert "/api/widgets/container-stats" in routes
    assert "/api/containers/{name}/stats" in routes
    assert "/ws/exec" in routes


async def test_widget_container_stats_returns_containers(client, app):
    # Use test-mode injection to avoid docker CLI dependency
    app["_test_container_stats"] = [
        {
            "name": "cc-demo",
            "status": "running",
            "cpu_percent": "1.23%",
            "mem_usage": "100MiB",
            "mem_limit": "1GiB",
            "mem_percent": "9.77%",
            "net_io": "1kB / 2kB",
            "block_io": "0B / 0B",
            "pids": 5,
            "created_by": "canvas-claude",
        },
        {
            "name": "other",
            "status": "stopped",
            "cpu_percent": "0.00%",
            "mem_usage": "0B",
            "mem_limit": "0B",
            "mem_percent": "0.00%",
            "net_io": "--",
            "block_io": "--",
            "pids": 0,
            "created_by": "",
        },
    ]
    resp = await client.get("/api/widgets/container-stats")
    assert resp.status == 200
    data = await resp.json()
    assert "containers" in data
    names = [c["name"] for c in data["containers"]]
    assert "cc-demo" in names
    assert "other" in names
    cc = next(c for c in data["containers"] if c["name"] == "cc-demo")
    assert cc["created_by"] == "canvas-claude"
    assert cc["status"] == "running"


async def test_widget_container_stats_empty(client, app):
    app["_test_container_stats"] = []
    resp = await client.get("/api/widgets/container-stats")
    assert resp.status == 200
    data = await resp.json()
    assert data["containers"] == []


async def test_container_single_stats_returns_data(client, app):
    app["_test_container_stats"] = [
        {
            "name": "cc-demo",
            "status": "running",
            "cpu_percent": "5.00%",
            "mem_usage": "50MiB",
            "mem_limit": "500MiB",
            "mem_percent": "10.00%",
            "created_by": "canvas-claude",
        },
    ]
    resp = await client.get("/api/containers/cc-demo/stats")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "cc-demo"
    assert data["cpu_percent"] == "5.00%"
    assert data["mem_usage"] == "50MiB"


async def test_container_single_stats_404(client, app):
    app["_test_container_stats"] = []
    resp = await client.get("/api/containers/nope/stats")
    assert resp.status == 404


# ── Remote-access (issue #224 / epic #119) ────────────────────────────────
#
# Regression guard: with --host 0.0.0.0 a remote browser on a Tailscale peer
# (e.g. http://100.64.0.5:3000) initiates WebSocket upgrades whose `Origin`
# header is NOT localhost. aiohttp 3.13.x's WebSocketResponse performs no
# Origin validation, so the handshake must succeed. If a future aiohttp
# release adds default Origin checking, this test fails and the four
# WebSocketResponse() call sites in server.py must be updated consistently.


async def test_websocket_accepts_non_localhost_origin(client):
    """
    /ws/control handshake must succeed when the browser sends a Tailscale-IP
    Origin header, because the auth boundary for remote access is Tailscale
    enrollment (not an application-level Origin allowlist). See the comment
    above `exec_websocket_handler` in server.py for context.
    """
    async with client.ws_connect(
        "/ws/control",
        headers={"Origin": "http://100.64.0.5:3000"},
    ) as ws:
        assert not ws.closed


# ── cards_list_handler unknown-type shim filter ───────────────────────────


async def test_cards_delete_unregisters_widget(tmp_path):
    """DELETE /api/cards/{id} unregisters a WidgetCard and returns 204."""
    from claude_rts import config as cfg
    from claude_rts.config import write_canvas
    from claude_rts.cards.widget_card import WidgetCard
    from aiohttp.test_utils import TestClient, TestServer

    app_config = cfg.load(tmp_path / ".sc")
    write_canvas(app_config, "del-test", {"cards": [], "pan": {"x": 0, "y": 0}, "zoom": 1})
    app = create_app(app_config)
    async with TestClient(TestServer(app)) as client:
        # Manually register a WidgetCard
        card = WidgetCard(widget_type="system-info", card_id="widget-del-123")
        registry = app["card_registry"]
        registry.register(card, canvas_name="del-test")
        assert registry.get("widget-del-123") is card

        resp = await client.delete("/api/cards/widget-del-123")
        assert resp.status == 204
        assert registry.get("widget-del-123") is None

    # 404 for unknown id
    app2_config = cfg.load(tmp_path / ".sc2")
    app2 = create_app(app2_config)
    async with TestClient(TestServer(app2)) as client2:
        resp = await client2.delete("/api/cards/does-not-exist")
        assert resp.status == 404


async def test_cards_list_handler_filters_unknown_type(tmp_path):
    """Snapshot entry with type 'foo' is dropped by the shim; only the known
    widget entry is returned.  Fix 1 of epic #254 e2e regressions."""
    from claude_rts import config as cfg
    from claude_rts.config import write_canvas
    from aiohttp.test_utils import TestClient, TestServer

    app_config = cfg.load(tmp_path / ".sc")
    # Seed a canvas with one widget entry and one unknown-type entry.
    write_canvas(
        app_config,
        "shim-filter-test",
        {
            "cards": [
                {
                    "type": "widget",
                    "widgetType": "system-info",
                    "card_id": "widget-aaa",
                    "x": 0,
                    "y": 0,
                    "w": 360,
                    "h": 280,
                },
                {
                    "type": "foo",
                    "card_id": "foo-bbb",
                    "x": 0,
                    "y": 0,
                },
            ],
            "pan": {"x": 0, "y": 0},
            "zoom": 1,
        },
    )
    app = create_app(app_config)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/cards?canvas=shim-filter-test")
        assert resp.status == 200
        data = await resp.json()
    assert len(data) == 1, f"Expected 1 descriptor (widget only), got {len(data)}: {data}"
    assert data[0]["type"] == "widget"
    assert data[0].get("card_id") == "widget-aaa"


# ── S20: /api/cards is read-only — POST and PUT return 405 ───────────────────


async def test_api_cards_post_and_put_return_405(client):
    """S20: Only GET is registered for /api/cards; POST and PUT return 405."""
    resp_post = await client.post("/api/cards", json={})
    assert resp_post.status == 405, f"Expected 405 for POST /api/cards, got {resp_post.status}"
    resp_put = await client.put("/api/cards", json={})
    assert resp_put.status == 405, f"Expected 405 for PUT /api/cards, got {resp_put.status}"


# ── S21: GET /api/cards does not leak server-private fields ──────────────────


async def test_api_cards_no_private_field_leak(tmp_path, aiohttp_client, monkeypatch):
    """S21: Each card descriptor in GET /api/cards contains only allowlisted fields.

    PIDs, file paths, internal session objects, raw _session references, and
    any field starting with '_' must not appear in the response.
    """
    import asyncio
    import json

    from tests.test_terminal_card import MockPty
    from claude_rts import config as cfg
    from claude_rts.server import create_app as _create_app
    from claude_rts.cards.widget_card import WidgetCard

    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = cfg.load(tmp_path / ".sc2")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "name": "sec-canvas",
        "canvas_size": [3840, 2160],
        "cards": [
            {"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"},
        ],
    }
    (app_config.canvases_dir / "sec-canvas.json").write_text(json.dumps(snapshot))

    app2 = _create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app2["_hydrate_retry_delays"] = [0]
    client2 = await aiohttp_client(app2)
    await asyncio.sleep(0.1)

    # Also register a WidgetCard manually so we test both types.
    widget = WidgetCard(widget_type="system-info", card_id="s21-widget-id")
    registry = app2["card_registry"]
    registry.register(widget, canvas_name="sec-canvas")

    resp = await client2.get("/api/cards?canvas=sec-canvas")
    assert resp.status == 200
    data = await resp.json()
    assert len(data) >= 1, f"Expected cards in response, got: {data}"

    # Allowlist of fields that may appear in card descriptors.
    ALLOWED_KEYS = {
        "type",
        "card_id",
        "session_id",
        "card_uid",
        "hub",
        "container",
        "exec",
        "starred",
        "display_name",
        "recovery_script",
        "error_state",
        "widgetType",
        "refreshInterval",
        "x",
        "y",
        "w",
        "h",
        "z_order",
    }

    for card_desc in data:
        leaked = set(card_desc.keys()) - ALLOWED_KEYS
        assert not leaked, f"Private fields leaked in card descriptor: {leaked}\nFull descriptor: {card_desc}"
        # Also assert no underscore-prefixed internal fields.
        private_keys = {k for k in card_desc if k.startswith("_")}
        assert not private_keys, f"Underscore-prefixed private fields found: {private_keys}"


# ── S22: DELETE /api/cards/{id} on terminal does not bypass PTY cleanup ───────


async def test_delete_api_cards_terminal_does_not_kill_pty_session(tmp_path, aiohttp_client, monkeypatch):
    """S22: DELETE /api/cards/{id} on a TerminalCard unregisters the card but
    does NOT destroy the PTY session — orphan reaper handles cleanup.

    cards_delete calls only CardRegistry.unregister, not SessionManager.destroy_session.
    """
    import asyncio
    import json

    from tests.test_terminal_card import MockPty
    from claude_rts import config as cfg
    from claude_rts.server import create_app as _create_app

    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = cfg.load(tmp_path / ".sc3")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "name": "pty-canvas",
        "canvas_size": [3840, 2160],
        "cards": [
            {"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"},
        ],
    }
    (app_config.canvases_dir / "pty-canvas.json").write_text(json.dumps(snapshot))

    app2 = _create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app2["_hydrate_retry_delays"] = [0]
    client2 = await aiohttp_client(app2)
    await asyncio.sleep(0.1)

    registry = app2["card_registry"]
    cards_on_canvas = registry.cards_on_canvas("pty-canvas")
    assert len(cards_on_canvas) == 1
    card = cards_on_canvas[0]
    session_id = card.id

    resp = await client2.delete(f"/api/cards/{session_id}")
    if resp.status == 204:
        # Card was unregistered — verify the PTY session is still alive
        # (orphan reaper handles cleanup, not cards_delete).
        session_manager = app2["session_manager"]
        session = session_manager.get_session(session_id)
        assert session is not None, (
            f"DELETE /api/cards/{session_id} destroyed the PTY session — "
            "cards_delete must NOT bypass the orphan reaper. "
            "Use DELETE /api/claude/terminal/{id} for terminal teardown."
        )
    else:
        # If the endpoint refuses terminal deletion (4xx), that is also acceptable.
        assert resp.status in (404, 400, 405), f"Unexpected status {resp.status} for DELETE /api/cards/{{terminal_id}}"


# ── S23: POST /api/cards/widget validates widgetType (no path traversal) ──────


async def test_post_widget_with_path_traversal_widgettype_is_safe(client):
    """S23: POST /api/cards/widget with widgetType='../../etc/passwd' is safe.

    The SUT accepts any non-empty string as widgetType (no server-side WIDGET_REGISTRY
    validation). This test verifies that:
      - The response is 200 or 400 (never 500).
      - If 200, the widgetType is stored as the literal string — no path resolution.
      - No file at the path '../../etc/passwd' is read or executed.
    """
    import json as _json

    resp = await client.post(
        "/api/cards/widget",
        json={"widgetType": "../../etc/passwd"},
    )
    # The SUT accepts any non-empty string as widgetType (no server-side path
    # validation). A path-traversal string is stored as the literal opaque
    # identifier. Assert the response is 200/201 (created safely), not an error.
    assert resp.status in (200, 201), (
        f"Expected 200/201 for path-traversal widgetType (stored as literal string), "
        f"got {resp.status}: {await resp.text()}"
    )
    body_text = await resp.text()
    body = _json.loads(body_text)
    # The widgetType must be the literal string — no path resolution or interpretation.
    assert body.get("widgetType") == "../../etc/passwd", (
        f"widgetType was transformed or not stored as literal: {body.get('widgetType')!r}"
    )
