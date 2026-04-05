"""Tests for the utility container module and claude-usage widget API."""

import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from claude_rts.server import create_app
from claude_rts.util_container import parse_json_from_output


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


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


async def test_claude_usage_with_profiles(client):
    """When profiles exist, return usage data."""
    mock_usage = {
        "five_hour_pct": 42.0,
        "five_hour_resets": "11pm (UTC)",
        "seven_day_pct": 25.0,
        "seven_day_resets": "Apr 7, 3pm (UTC)",
        "sonnet_week_pct": 10.0,
        "sonnet_week_resets": "Apr 7, 5pm (UTC)",
    }
    with patch("claude_rts.server.is_util_running", new_callable=AsyncMock, return_value=True), \
         patch("claude_rts.server.list_profiles", new_callable=AsyncMock, return_value=["acct-alice"]), \
         patch("claude_rts.server.probe_usage_via_session", new_callable=AsyncMock, return_value=mock_usage):
        resp = await client.get("/api/widgets/claude-usage")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert len(data["profiles"]) == 1
    assert data["profiles"][0]["profile"] == "acct-alice"
    assert data["profiles"][0]["five_hour_pct"] == 42.0


async def test_claude_usage_probe_failure(client):
    """When a probe fails with no stale data, return error for that profile."""
    with patch("claude_rts.server.is_util_running", new_callable=AsyncMock, return_value=True), \
         patch("claude_rts.server.list_profiles", new_callable=AsyncMock, return_value=["acct-bob"]), \
         patch("claude_rts.server.probe_usage_via_session", new_callable=AsyncMock, return_value=None):
        resp = await client.get("/api/widgets/claude-usage")
    assert resp.status == 200
    data = await resp.json()
    assert data["profiles"][0]["error"] == "probe failed"


# ── parse_json_from_output unit tests ──


def test_parse_json_clean():
    assert parse_json_from_output('{"five_hour_pct": 42.0}') == {"five_hour_pct": 42.0}


def test_parse_json_with_ansi():
    raw = '\x1b[32m{"five_hour_pct": 10.0}\x1b[0m'
    assert parse_json_from_output(raw) == {"five_hour_pct": 10.0}


def test_parse_json_with_surrounding_text():
    raw = "some preamble\r\n{\"key\": \"val\"}\r\nextra output"
    assert parse_json_from_output(raw) == {"key": "val"}


def test_parse_json_no_json():
    assert parse_json_from_output("WARNING: No usage data parsed") is None


def test_parse_json_malformed():
    assert parse_json_from_output("{not valid json}") is None


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
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, 'resource')]
    assert "/api/widgets/claude-usage" in routes
    assert "/api/widgets/claude-usage/status" in routes
