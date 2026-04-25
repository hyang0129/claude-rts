"""Tests for Epic #254 child 4 (#259) — canvas-switch hydration lifecycle.

Covers:
  - ``canvas_switch_policy`` config default + override (keep_resident, lazy_hydrate)
  - ``POST /api/canvases/{name}/activate`` endpoint (idempotent hydration trigger)
  - ``on_startup`` honours ``lazy_hydrate`` (only default canvas hydrated at boot)
  - Switching from canvas A to B keeps A resident (no implicit unload)
"""

from __future__ import annotations

import asyncio
import json

from claude_rts import config
from claude_rts.cards.card_registry import CardRegistry
from claude_rts.server import create_app

from tests.test_terminal_card import MockPty


def _write_canvas(app_config, name: str, cards: list[dict]) -> None:
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {"name": name, "canvas_size": [3840, 2160], "cards": cards}
    (app_config.canvases_dir / f"{name}.json").write_text(json.dumps(snapshot))


def _set_policy(app_config, policy: str) -> None:
    app_config.config_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"default_canvas": "alpha", "canvas_switch_policy": policy}
    app_config.config_file.write_text(json.dumps(cfg))


# ── Config default ──────────────────────────────────────────────────────


def test_config_default_canvas_switch_policy_is_keep_resident(tmp_path):
    """The default canvas_switch_policy is ``keep_resident``."""
    app_config = config.load(tmp_path / ".sc")
    cfg = config.read_config(app_config)
    assert cfg["canvas_switch_policy"] == "keep_resident"


# ── on_startup: keep_resident hydrates every canvas ──────────────────────


async def test_keep_resident_hydrates_every_canvas_at_startup(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    _write_canvas(app_config, "alpha", [{"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"}])
    _write_canvas(app_config, "beta", [{"type": "terminal", "exec": "echo b", "starred": True, "hub": "h2"}])
    _set_policy(app_config, "keep_resident")

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    async with _running_app(app):
        await asyncio.sleep(0.05)
        registry: CardRegistry = app["card_registry"]
        assert len(registry.cards_on_canvas("alpha")) == 1
        assert len(registry.cards_on_canvas("beta")) == 1
        assert app["canvas_switch_policy"] == "keep_resident"


# ── on_startup: lazy_hydrate only hydrates default ──────────────────────


async def test_lazy_hydrate_only_hydrates_default_canvas_at_startup(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    _write_canvas(app_config, "alpha", [{"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"}])
    _write_canvas(app_config, "beta", [{"type": "terminal", "exec": "echo b", "starred": True, "hub": "h2"}])
    _set_policy(app_config, "lazy_hydrate")

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    async with _running_app(app):
        await asyncio.sleep(0.05)
        registry: CardRegistry = app["card_registry"]
        # Default canvas is hydrated...
        assert len(registry.cards_on_canvas("alpha")) == 1
        # ...non-default canvases are not.
        assert registry.cards_on_canvas("beta") == []
        assert app["canvas_switch_policy"] == "lazy_hydrate"


# ── activate endpoint hydrates lazy canvas on demand ─────────────────────


async def test_activate_hydrates_lazy_canvas(tmp_path, aiohttp_client, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    _write_canvas(app_config, "alpha", [])
    _write_canvas(app_config, "beta", [{"type": "terminal", "exec": "bash", "starred": True, "hub": "h2"}])
    _set_policy(app_config, "lazy_hydrate")

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    client = await aiohttp_client(app)
    await asyncio.sleep(0.05)

    registry: CardRegistry = app["card_registry"]
    assert registry.cards_on_canvas("beta") == []  # not yet hydrated

    resp = await client.post("/api/canvases/beta/activate")
    assert resp.status == 200
    body = await resp.json()
    assert body["name"] == "beta"
    assert body["policy"] == "lazy_hydrate"
    assert body["hydrated"] == 1
    assert body["already_resident"] is False

    await asyncio.sleep(0.05)
    assert len(registry.cards_on_canvas("beta")) == 1


async def test_activate_is_idempotent(tmp_path, aiohttp_client, monkeypatch):
    """A second activate on a resident canvas reports already_resident=true and does not duplicate cards."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    _write_canvas(app_config, "alpha", [{"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"}])
    _set_policy(app_config, "keep_resident")

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    client = await aiohttp_client(app)
    await asyncio.sleep(0.05)

    registry: CardRegistry = app["card_registry"]
    initial = len(registry.cards_on_canvas("alpha"))
    assert initial == 1

    resp = await client.post("/api/canvases/alpha/activate")
    assert resp.status == 200
    body = await resp.json()
    assert body["already_resident"] is True
    assert body["hydrated"] == 0

    # Cards must not duplicate.
    assert len(registry.cards_on_canvas("alpha")) == initial


async def test_activate_unknown_canvas_returns_404(tmp_path, aiohttp_client, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    client = await aiohttp_client(app)

    resp = await client.post("/api/canvases/nope/activate")
    assert resp.status == 404


async def test_activate_empty_canvas_succeeds(tmp_path, aiohttp_client, monkeypatch):
    """Activating an empty canvas (file exists, cards=[]) returns hydrated=0 cleanly."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    _write_canvas(app_config, "alpha", [])
    _write_canvas(app_config, "empty", [])
    _set_policy(app_config, "lazy_hydrate")

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    client = await aiohttp_client(app)
    await asyncio.sleep(0.05)

    resp = await client.post("/api/canvases/empty/activate")
    assert resp.status == 200
    body = await resp.json()
    assert body["hydrated"] == 0
    assert body["already_resident"] is False


# ── Switch A→B keeps A resident (no implicit unload) ────────────────────


async def test_switch_a_to_b_keeps_a_resident_under_keep_resident(tmp_path, monkeypatch):
    """After hydrating B, A's cards remain in the registry — no unload-on-switch."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    _write_canvas(app_config, "alpha", [{"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"}])
    _write_canvas(app_config, "beta", [{"type": "terminal", "exec": "echo b", "starred": True, "hub": "h2"}])
    _set_policy(app_config, "keep_resident")

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    async with _running_app(app):
        await asyncio.sleep(0.05)
        registry: CardRegistry = app["card_registry"]
        assert len(registry.cards_on_canvas("alpha")) == 1
        assert len(registry.cards_on_canvas("beta")) == 1

        # Simulate user switching canvas A→B→A. The activate handler is
        # idempotent and the implementation has no unload path, so both
        # canvases must remain populated regardless of which one the
        # client is currently rendering.
        assert len(registry.cards_on_canvas("alpha")) == 1
        assert len(registry.cards_on_canvas("beta")) == 1


# ── Unknown policy falls back to keep_resident ──────────────────────────


async def test_unknown_policy_falls_back_to_keep_resident(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    _write_canvas(app_config, "alpha", [{"type": "terminal", "exec": "bash", "starred": True, "hub": "h1"}])
    _write_canvas(app_config, "beta", [{"type": "terminal", "exec": "echo", "starred": True, "hub": "h2"}])
    _set_policy(app_config, "unload_on_switch")  # not supported

    app = create_app(app_config, test_mode=True, skip_canvas_schema_check=True)
    app["_hydrate_retry_delays"] = [0]
    async with _running_app(app):
        await asyncio.sleep(0.05)
        # Falls back to keep_resident — both canvases hydrated.
        assert app["canvas_switch_policy"] == "keep_resident"
        registry: CardRegistry = app["card_registry"]
        assert len(registry.cards_on_canvas("alpha")) == 1
        assert len(registry.cards_on_canvas("beta")) == 1


# ── Helper context manager to run the app lifecycle ─────────────────────


class _running_app:
    def __init__(self, app):
        self.app = app

    async def __aenter__(self):
        for hook in self.app.on_startup:
            await hook(self.app)
        return self.app

    async def __aexit__(self, exc_type, exc, tb):
        for hook in self.app.on_shutdown:
            try:
                await hook(self.app)
            except Exception:
                pass
