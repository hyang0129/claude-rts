"""Tests for the /api/profiles endpoints (issue #72).

Tests the profile manager API:
- GET /api/profiles lists probe profiles with latest usage data
- GET /api/profiles/priority returns the current priority profile
- PUT /api/profiles/priority sets the priority profile
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
    with patch("claude_rts.server.discover_profiles", new_callable=AsyncMock, return_value=[]):
        yield await aiohttp_client(app)


def _make_mock_card(last_result=_FAKE_RESULT):
    card = MagicMock()
    card.last_result = last_result
    return card


# ── GET /api/profiles ────────────────────────────────────────────────────────


async def test_profiles_list_empty(client, tmp_path):
    """No probe_profiles and no discovered profiles returns empty list."""

    resp = await client.get("/api/profiles")
    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_profiles_list_with_data(client, tmp_path):
    """Profiles with probe data are returned sorted by burn_rate ascending."""

    app_config = client.app["app_config"]
    cfg = config.read_config(app_config)
    cfg["probe_profiles"] = ["alice", "bob"]
    config.write_config(app_config, cfg)

    card_alice = _make_mock_card({**_FAKE_RESULT, "profile": "alice", "burn_rate": 50.0})
    card_bob = _make_mock_card({**_FAKE_RESULT, "profile": "bob", "burn_rate": 10.0})

    def fake_get(card_type, identity):
        return {"alice": card_alice, "bob": card_bob}.get(identity)

    with patch("claude_rts.server.ServiceCardRegistry.get", side_effect=fake_get):
        resp = await client.get("/api/profiles")

    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 2
    # bob (burn_rate=10) should be first (ascending sort)
    assert data[0]["profile"] == "bob"
    assert data[0]["burn_rate"] == 10.0
    assert data[1]["profile"] == "alice"
    assert data[1]["burn_rate"] == 50.0


async def test_profiles_list_no_probe_result(client, tmp_path):
    """Profile with no probe result shows probe_available=False."""

    app_config = client.app["app_config"]
    cfg = config.read_config(app_config)
    cfg["probe_profiles"] = ["hongy"]
    config.write_config(app_config, cfg)

    card = _make_mock_card(last_result=None)

    with patch("claude_rts.server.ServiceCardRegistry.get", return_value=card):
        resp = await client.get("/api/profiles")

    data = await resp.json()
    assert len(data) == 1
    assert data[0]["probe_available"] is False
    assert data[0]["five_hour_pct"] is None


async def test_profiles_list_priority_flag(client, tmp_path):
    """Priority profile is marked with is_priority=True."""
    app_config = client.app["app_config"]
    cfg = config.read_config(app_config)
    cfg["probe_profiles"] = ["alice", "bob"]
    cfg["priority_profile"] = "bob"
    config.write_config(app_config, cfg)

    with patch("claude_rts.server.ServiceCardRegistry.get", return_value=None):
        resp = await client.get("/api/profiles")

    data = await resp.json()
    alice = next(p for p in data if p["profile"] == "alice")
    bob = next(p for p in data if p["profile"] == "bob")
    assert alice["is_priority"] is False
    assert bob["is_priority"] is True


# ── GET /api/profiles/priority ───────────────────────────────────────────────


async def test_priority_get_default(client):
    """Fresh config returns priority_profile=null."""
    resp = await client.get("/api/profiles/priority")
    assert resp.status == 200
    data = await resp.json()
    assert data["priority_profile"] is None


# ── PUT /api/profiles/priority ───────────────────────────────────────────────


async def test_priority_put_valid(client):
    """Set priority to a valid profile, verify GET returns it."""
    app_config = client.app["app_config"]
    cfg = config.read_config(app_config)
    cfg["probe_profiles"] = ["hongy"]
    config.write_config(app_config, cfg)

    resp = await client.put(
        "/api/profiles/priority",
        json={"priority_profile": "hongy"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["priority_profile"] == "hongy"

    # Verify persistence via GET
    resp2 = await client.get("/api/profiles/priority")
    data2 = await resp2.json()
    assert data2["priority_profile"] == "hongy"


async def test_priority_put_invalid_profile(client):
    """Setting priority to a profile not in probe_profiles returns 400."""
    resp = await client.put(
        "/api/profiles/priority",
        json={"priority_profile": "nonexistent"},
    )
    assert resp.status == 400


async def test_priority_put_null_clears(client):
    """Setting priority_profile to null clears it."""
    app_config = client.app["app_config"]
    cfg = config.read_config(app_config)
    cfg["probe_profiles"] = ["hongy"]
    cfg["priority_profile"] = "hongy"
    config.write_config(app_config, cfg)

    resp = await client.put(
        "/api/profiles/priority",
        json={"priority_profile": None},
    )
    assert resp.status == 200

    resp2 = await client.get("/api/profiles/priority")
    data2 = await resp2.json()
    assert data2["priority_profile"] is None


# ── Route registration ───────────────────────────────────────────────────────


async def test_app_has_profiles_routes(app):
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/api/profiles" in routes
    assert "/api/profiles/priority" in routes
