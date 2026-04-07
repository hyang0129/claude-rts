"""Tests for the POST /api/claude-usage endpoint (issue #80).

Tests the "create or reuse" service card lifecycle:
- First call for a profile creates a ClaudeUsageCard and returns its result
- Subsequent calls for the same profile reuse the existing card
- Missing/invalid body returns 400
- Failed probe returns 503
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_rts import config
from claude_rts.server import create_app


_FAKE_RESULT = {
    "profile": "hongy",
    "five_hour_pct": 42.0,
    "five_hour_resets": "in 1h 30m",
    "seven_day_pct": 20.0,
    "seven_day_resets": "in 5 days",
    "burn_rate": 28.0,
}


@pytest.fixture
def app(tmp_path):
    return create_app(config.load(tmp_path / ".sc"))


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


def _make_mock_card(last_result=_FAKE_RESULT):
    card = MagicMock()
    card.last_result = last_result
    return card


# ── Missing / invalid body ────────────────────────────────────────────────────


async def test_claude_usage_missing_body_returns_400(client):
    resp = await client.post("/api/claude-usage", data="not json", headers={"Content-Type": "application/json"})
    assert resp.status == 400


async def test_claude_usage_missing_profile_field_returns_400(client):
    resp = await client.post("/api/claude-usage", json={})
    assert resp.status == 400
    text = await resp.text()
    assert "profile" in text


# ── First call: card does not exist yet ───────────────────────────────────────


async def test_claude_usage_first_call_creates_card(client):
    """First call: registry.get() returns None, subscribe() creates and starts the card."""
    mock_card = _make_mock_card()

    with (
        patch("claude_rts.server.ServiceCardRegistry.get", return_value=None),
        patch("claude_rts.server.ServiceCardRegistry.subscribe", new_callable=AsyncMock, return_value=mock_card),
    ):
        resp = await client.post("/api/claude-usage", json={"profile": "hongy"})

    assert resp.status == 200
    data = await resp.json()
    assert data["profile"] == "hongy"
    assert data["five_hour_pct"] == 42.0


async def test_claude_usage_first_call_subscribe_called_with_profile(client):
    """subscribe() is called with the correct profile as identity."""
    mock_card = _make_mock_card()
    captured = {}

    async def fake_subscribe(card_type, identity, callback, **kwargs):
        captured["card_type"] = card_type
        captured["identity"] = identity
        return mock_card

    with (
        patch("claude_rts.server.ServiceCardRegistry.get", return_value=None),
        patch("claude_rts.server.ServiceCardRegistry.subscribe", side_effect=fake_subscribe),
    ):
        await client.post("/api/claude-usage", json={"profile": "hongy"})

    assert captured["card_type"] == "claude-usage"
    assert captured["identity"] == "hongy"


# ── Subsequent calls: card already exists ─────────────────────────────────────


async def test_claude_usage_subsequent_call_reuses_card(client):
    """Subsequent call: registry.get() returns existing card, subscribe() not called."""
    mock_card = _make_mock_card()

    with (
        patch("claude_rts.server.ServiceCardRegistry.get", return_value=mock_card),
        patch("claude_rts.server.ServiceCardRegistry.subscribe", new_callable=AsyncMock) as mock_subscribe,
    ):
        resp = await client.post("/api/claude-usage", json={"profile": "hongy"})

    assert resp.status == 200
    mock_subscribe.assert_not_called()
    data = await resp.json()
    assert data["five_hour_pct"] == 42.0


async def test_claude_usage_subsequent_call_returns_last_result(client):
    """last_result from the existing card is returned directly."""
    expected = {**_FAKE_RESULT, "five_hour_pct": 99.0}
    mock_card = _make_mock_card(last_result=expected)

    with patch("claude_rts.server.ServiceCardRegistry.get", return_value=mock_card):
        resp = await client.post("/api/claude-usage", json={"profile": "hongy"})

    data = await resp.json()
    assert data["five_hour_pct"] == 99.0


# ── Failed probe ──────────────────────────────────────────────────────────────


async def test_claude_usage_returns_503_when_probe_failed(client):
    """If last_result is None (probe failed), endpoint returns 503."""
    mock_card = _make_mock_card(last_result=None)

    with (
        patch("claude_rts.server.ServiceCardRegistry.get", return_value=None),
        patch("claude_rts.server.ServiceCardRegistry.subscribe", new_callable=AsyncMock, return_value=mock_card),
    ):
        resp = await client.post("/api/claude-usage", json={"profile": "hongy"})

    assert resp.status == 503
    data = await resp.json()
    assert "error" in data


# ── Route registration ────────────────────────────────────────────────────────


async def test_app_has_claude_usage_route(app):
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/api/claude-usage" in routes
