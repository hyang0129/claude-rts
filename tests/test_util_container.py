"""Tests for the utility container module and claude-usage widget API.

The backend never probes directly. All probe data flows from the frontend
credential-manager widget via POST /api/credentials/{name}/probe-result.
The widget_claude_usage_handler reads exclusively from CredentialManager cache.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_rts.profile_manager import CredentialState
from claude_rts.server import create_app


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


def _make_state(name: str, **kwargs) -> CredentialState:
    defaults = dict(
        usage_5hr_pct=42.0,
        usage_daily_pct=20.0,
        five_hour_resets="2h 0m",
        seven_day_resets="Apr 7, 3pm (UTC)",
        burn_rate=15.0,
        burn_class="normal",
        health="healthy",
        account_id="acct-111",
        data_timestamp=1234567890.0,
    )
    defaults.update(kwargs)
    return CredentialState(name=name, **defaults)


# ── Claude usage widget API tests ──


async def test_claude_usage_util_not_running(client):
    """When utility container is not running, return error."""
    with patch("claude_rts.server.is_util_running", new_callable=AsyncMock, return_value=False):
        resp = await client.get("/api/widgets/claude-usage")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "error"
    assert "not running" in data["error"]


async def test_claude_usage_no_profiles(client):
    """When no profiles found, return empty list."""
    with patch("claude_rts.server.is_util_running", new_callable=AsyncMock, return_value=True), \
         patch("claude_rts.server.list_profiles", new_callable=AsyncMock, return_value=[]):
        resp = await client.get("/api/widgets/claude-usage")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["profiles"] == []


async def test_claude_usage_with_cached_probe_data(client):
    """When profiles exist and CredentialManager has cached data, return it."""
    state = _make_state("acct-alice")
    cred_mgr = MagicMock()
    cred_mgr.get.return_value = state

    with patch("claude_rts.server.is_util_running", new_callable=AsyncMock, return_value=True), \
         patch("claude_rts.server.list_profiles", new_callable=AsyncMock, return_value=["acct-alice"]):
        client.app["credential_manager"] = cred_mgr
        resp = await client.get("/api/widgets/claude-usage")

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert len(data["profiles"]) == 1
    p = data["profiles"][0]
    assert p["profile"] == "acct-alice"
    assert p["five_hour_pct"] == 42.0
    assert p["cached"] is True


async def test_claude_usage_uncached_profile(client):
    """When profile exists but CredentialManager has no data, return cached=False."""
    cred_mgr = MagicMock()
    cred_mgr.get.return_value = None  # no cached data yet

    with patch("claude_rts.server.is_util_running", new_callable=AsyncMock, return_value=True), \
         patch("claude_rts.server.list_profiles", new_callable=AsyncMock, return_value=["acct-bob"]):
        client.app["credential_manager"] = cred_mgr
        resp = await client.get("/api/widgets/claude-usage")

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert len(data["profiles"]) == 1
    assert data["profiles"][0] == {"profile": "acct-bob", "cached": False}


async def test_claude_usage_status_endpoint(client):
    """Status endpoint returns util container state."""
    with patch("claude_rts.server.is_util_running", new_callable=AsyncMock, return_value=True), \
         patch("claude_rts.server.list_profiles", new_callable=AsyncMock, return_value=["acct-1", "acct-2"]):
        resp = await client.get("/api/widgets/claude-usage/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["util_running"] is True
    assert data["profile_count"] == 2
    assert data["profiles"] == ["acct-1", "acct-2"]


async def test_app_has_claude_usage_routes(app):
    """Verify claude-usage routes are registered."""
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/api/widgets/claude-usage" in routes
    assert "/api/widgets/claude-usage/status" in routes
