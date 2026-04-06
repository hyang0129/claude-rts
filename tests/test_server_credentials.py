"""Tests for the Credential Manager API endpoints in claude_rts.server."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from claude_rts.profile_manager import CredentialState
from claude_rts.server import create_app


# -- Fixtures -----------------------------------------------------------------


def _make_state(name: str, **kwargs) -> CredentialState:
    """Return a CredentialState with sensible defaults."""
    defaults = dict(
        usage_5hr_pct=40.0,
        usage_daily_pct=20.0,
        five_hour_resets="2h 0m",
        seven_day_resets=None,
        burn_rate=20.0,
        burn_class="normal",
        health="healthy",
        account_id="acct-111",
        last_probe_time=None,
        last_health_check=None,
        error=None,
    )
    defaults.update(kwargs)
    return CredentialState(name=name, **defaults)


def _make_credential_manager(states=None) -> MagicMock:
    """Return a MagicMock CredentialManager pre-loaded with the given states."""
    mgr = MagicMock()
    states = states or []
    cache = {s.name: s for s in states}

    mgr.get_all.return_value = states
    mgr.get.side_effect = lambda name: cache.get(name)
    mgr.get_best.return_value = states[0] if states else None
    mgr.force_health_check = AsyncMock(return_value=states[0] if states else _make_state("default"))
    mgr.create_profile = AsyncMock(return_value={"success": True, "name": "new-profile"})
    mgr.delete_profile = AsyncMock(return_value=True)
    return mgr


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client_with_manager(aiohttp_client, app):
    """Create a test client whose credential_manager is set via on_startup injection."""
    states = [_make_state("acct-alice", burn_rate=10.0), _make_state("acct-bob", burn_rate=20.0)]
    mgr = _make_credential_manager(states)

    async def _inject_manager(app_: web.Application) -> None:
        app_["credential_manager"] = mgr

    app.on_startup.append(_inject_manager)

    with patch("claude_rts.server.ensure_util_container", new=AsyncMock()), \
         patch("claude_rts.server.SessionManager") as MockSM, \
         patch("claude_rts.server.discover_hubs", new=AsyncMock(return_value=[])):
        mock_sm_inst = MagicMock()
        mock_sm_inst.start_orphan_reaper = MagicMock()
        mock_sm_inst.tmux_enabled = False
        mock_sm_inst.list_sessions.return_value = []
        MockSM.return_value = mock_sm_inst

        with patch("claude_rts.server.CredentialManager") as MockCM:
            MockCM.return_value = MagicMock()
            client = await aiohttp_client(app)

    client.app["credential_manager"] = mgr
    return client, mgr


# -- Helper -------------------------------------------------------------------


async def _make_client(aiohttp_client, mgr: MagicMock):
    """Build a minimal aiohttp test client with credential routes and the mock injected."""
    bare_app = web.Application()

    from claude_rts.server import (
        credentials_best_handler,
        credentials_check_handler,
        credentials_create_handler,
        credentials_delete_handler,
        credentials_get_handler,
        credentials_list_handler,
        credentials_probe_handler,
        credentials_probe_result_handler,
    )

    bare_app.router.add_get("/api/credentials", credentials_list_handler)
    bare_app.router.add_get("/api/credentials/best", credentials_best_handler)
    bare_app.router.add_get("/api/credentials/{name}", credentials_get_handler)
    bare_app.router.add_post("/api/credentials", credentials_create_handler)
    bare_app.router.add_delete("/api/credentials/{name}", credentials_delete_handler)
    bare_app.router.add_post("/api/credentials/{name}/probe", credentials_probe_handler)
    bare_app.router.add_post("/api/credentials/{name}/probe-result", credentials_probe_result_handler)
    bare_app.router.add_post("/api/credentials/{name}/check", credentials_check_handler)

    bare_app["credential_manager"] = mgr
    return await aiohttp_client(bare_app)


# -- GET /api/credentials -----------------------------------------------------


async def test_credentials_list(aiohttp_client):
    """GET /api/credentials returns 200 with credentials list."""
    states = [_make_state("acct-alice"), _make_state("acct-bob")]
    mgr = _make_credential_manager(states)
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.get("/api/credentials")
    assert resp.status == 200
    data = await resp.json()
    assert "credentials" in data
    assert len(data["credentials"]) == 2
    names = {c["name"] for c in data["credentials"]}
    assert names == {"acct-alice", "acct-bob"}


async def test_credentials_list_empty(aiohttp_client):
    """GET /api/credentials with empty cache returns 200 with empty list."""
    mgr = _make_credential_manager([])
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.get("/api/credentials")
    assert resp.status == 200
    data = await resp.json()
    assert data["credentials"] == []


async def test_credentials_list_includes_all_fields(aiohttp_client):
    """Each credential entry in the list includes expected fields."""
    states = [_make_state("acct-x")]
    mgr = _make_credential_manager(states)
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.get("/api/credentials")
    assert resp.status == 200
    data = await resp.json()
    cred = data["credentials"][0]
    assert "name" in cred
    assert "burn_rate" in cred
    assert "burn_class" in cred
    assert "health" in cred


# -- GET /api/credentials/{name} ----------------------------------------------


async def test_credentials_get_found(aiohttp_client):
    """GET /api/credentials/{name} with existing name returns 200."""
    state = _make_state("acct-alice", usage_5hr_pct=55.0)
    mgr = _make_credential_manager([state])
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.get("/api/credentials/acct-alice")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "acct-alice"
    assert data["usage_5hr_pct"] == 55.0


async def test_credentials_get_not_found(aiohttp_client):
    """GET /api/credentials/{name} for nonexistent profile returns 404."""
    mgr = _make_credential_manager([])
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.get("/api/credentials/no-such-profile")
    assert resp.status == 404


async def test_credentials_get_invalid_name(aiohttp_client):
    """GET /api/credentials/{name} with invalid chars returns 400."""
    mgr = _make_credential_manager([])
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.get("/api/credentials/bad.name")
    assert resp.status == 400
    data = await resp.json()
    assert "Invalid" in data["error"]


# -- POST /api/credentials ----------------------------------------------------


async def test_credentials_create_success(aiohttp_client):
    """POST /api/credentials with valid name returns 200 and success."""
    mgr = _make_credential_manager([])
    mgr.create_profile = AsyncMock(return_value={"success": True, "name": "acct-new"})
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.post("/api/credentials", json={"name": "acct-new"})
    assert resp.status == 200
    data = await resp.json()
    assert data["success"] is True
    assert data["name"] == "acct-new"


async def test_credentials_create_no_name(aiohttp_client):
    """POST /api/credentials with missing name field returns 400."""
    mgr = _make_credential_manager([])
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.post("/api/credentials", json={})
    assert resp.status == 400


async def test_credentials_create_empty_name(aiohttp_client):
    """POST /api/credentials with empty name string returns 400."""
    mgr = _make_credential_manager([])
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.post("/api/credentials", json={"name": "  "})
    assert resp.status == 400


async def test_credentials_create_already_exists(aiohttp_client):
    """POST /api/credentials when profile already exists returns 409."""
    state = _make_state("acct-existing")
    mgr = _make_credential_manager([state])
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.post("/api/credentials", json={"name": "acct-existing"})
    assert resp.status == 409


async def test_credentials_create_dir_failure(aiohttp_client):
    """POST /api/credentials returns 400 when create_profile fails."""
    mgr = _make_credential_manager([])
    mgr.create_profile = AsyncMock(
        return_value={"success": False, "error": "Failed to create profile directory"}
    )
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.post("/api/credentials", json={"name": "acct-bad"})
    assert resp.status == 400
    data = await resp.json()
    assert data["success"] is False


# -- DELETE /api/credentials/{name} -------------------------------------------


async def test_credentials_delete_success(aiohttp_client):
    """DELETE /api/credentials/{name} returns 200 on success."""
    state = _make_state("acct-todelete")
    mgr = _make_credential_manager([state])
    mgr.delete_profile = AsyncMock(return_value=True)
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.delete("/api/credentials/acct-todelete")
    assert resp.status == 200
    data = await resp.json()
    assert data["success"] is True


async def test_credentials_delete_not_found(aiohttp_client):
    """DELETE /api/credentials/{name} for nonexistent profile returns 404."""
    mgr = _make_credential_manager([])
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.delete("/api/credentials/ghost-profile")
    assert resp.status == 404


async def test_credentials_delete_dir_failure(aiohttp_client):
    """DELETE /api/credentials/{name} returns 500 when delete_profile fails."""
    state = _make_state("acct-stuck")
    mgr = _make_credential_manager([state])
    mgr.delete_profile = AsyncMock(return_value=False)
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.delete("/api/credentials/acct-stuck")
    assert resp.status == 500
    data = await resp.json()
    assert data["success"] is False


# -- POST /api/credentials/{name}/probe ---------------------------------------


async def test_credentials_probe(aiohttp_client):
    """POST /api/credentials/{name}/probe returns 200 with the updated state."""
    state = _make_state("acct-alice", burn_rate=12.0)
    mgr = _make_credential_manager([state])
    mgr.force_probe = AsyncMock(return_value=state)
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.post("/api/credentials/acct-alice/probe")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "acct-alice"
    assert data["burn_rate"] == 12.0
    mgr.force_probe.assert_awaited_once_with("acct-alice")


async def test_credentials_probe_unknown_profile(aiohttp_client):
    """POST .../probe works even for profiles not in cache."""
    new_state = _make_state("acct-fresh")
    mgr = _make_credential_manager([])
    mgr.force_probe = AsyncMock(return_value=new_state)
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.post("/api/credentials/acct-fresh/probe")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "acct-fresh"


# -- POST /api/credentials/{name}/check ---------------------------------------


async def test_credentials_check(aiohttp_client):
    """POST /api/credentials/{name}/check returns 200 with the updated health state."""
    state = _make_state("acct-bob", health="healthy")
    mgr = _make_credential_manager([state])
    mgr.force_health_check = AsyncMock(return_value=state)
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.post("/api/credentials/acct-bob/check")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "acct-bob"
    assert data["health"] == "healthy"
    mgr.force_health_check.assert_awaited_once_with("acct-bob")


async def test_credentials_check_marks_stale(aiohttp_client):
    """POST .../check returns 200 with health=stale when health check fails."""
    state = _make_state("acct-bad", health="stale")
    mgr = _make_credential_manager([])
    mgr.force_health_check = AsyncMock(return_value=state)
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.post("/api/credentials/acct-bad/check")
    assert resp.status == 200
    data = await resp.json()
    assert data["health"] == "stale"


# -- GET /api/credentials/best ------------------------------------------------


async def test_credentials_best_returns_profile(aiohttp_client):
    """GET /api/credentials/best returns 200 with profile info for the best credential."""
    state = _make_state("acct-alice", burn_rate=10.0, burn_class="normal", health="healthy")
    mgr = _make_credential_manager([state])
    mgr.get_best.return_value = state
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.get("/api/credentials/best")
    assert resp.status == 200
    data = await resp.json()
    assert data["profile"] == "acct-alice"
    assert data["burn_rate"] == 10.0
    assert data["burn_class"] == "normal"
    assert data["health"] == "healthy"
    assert "usage_5hr_pct" in data


async def test_credentials_best_all_stale(aiohttp_client):
    """GET /api/credentials/best returns 503 when all credentials are stale/unhealthy."""
    state = _make_state("acct-stale", health="stale")
    mgr = _make_credential_manager([state])
    mgr.get_best.return_value = None
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.get("/api/credentials/best")
    assert resp.status == 503
    data = await resp.json()
    assert data["error"] == "none_available"


async def test_credentials_best_empty(aiohttp_client):
    """GET /api/credentials/best returns 503 when cache is empty."""
    mgr = _make_credential_manager([])
    mgr.get_best.return_value = None
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.get("/api/credentials/best")
    assert resp.status == 503


# -- Route registration sanity check ------------------------------------------


async def test_credential_routes_are_registered(app):
    """Verify all credential management routes are registered in the main app."""
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/api/credentials" in routes
    assert "/api/credentials/best" in routes
    assert "/api/credentials/{name}" in routes
    assert "/api/credentials/{name}/probe" in routes
    assert "/api/credentials/{name}/probe-result" in routes
    assert "/api/credentials/{name}/check" in routes


# -- POST /api/credentials/{name}/probe-result --------------------------------
# This is the endpoint the frontend credential-manager widget calls after running
# claude-usage inside the utility container via a headed xterm.js WebSocket session.


async def test_credentials_probe_result_ingests_data(aiohttp_client):
    """POST /probe-result stores probe data and returns updated state."""
    from claude_rts.profile_manager import CredentialManager

    real_mgr = CredentialManager()
    mgr_mock = MagicMock()
    ingested = _make_state("acct-alice", usage_5hr_pct=55.0)
    mgr_mock.ingest_probe_result.return_value = ingested
    client = await _make_client(aiohttp_client, mgr_mock)

    payload = {
        "five_hour_pct": 55.0,
        "five_hour_resets": "1h 30m",
        "seven_day_pct": 30.0,
        "seven_day_resets": "Apr 7, 3pm (UTC)",
    }
    resp = await client.post("/api/credentials/acct-alice/probe-result", json=payload)
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "acct-alice"
    assert data["usage_5hr_pct"] == 55.0
    mgr_mock.ingest_probe_result.assert_called_once_with("acct-alice", payload)


async def test_credentials_probe_result_invalid_name(aiohttp_client):
    """POST /probe-result with invalid profile name returns 400."""
    mgr = _make_credential_manager([])
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.post("/api/credentials/bad.name/probe-result", json={"five_hour_pct": 10.0})
    assert resp.status == 400
    data = await resp.json()
    assert "Invalid" in data["error"]


async def test_credentials_probe_result_invalid_json(aiohttp_client):
    """POST /probe-result with non-JSON body returns 400."""
    mgr = _make_credential_manager([])
    client = await _make_client(aiohttp_client, mgr)

    resp = await client.post("/api/credentials/acct-ok/probe-result", data="not-json",
                             headers={"Content-Type": "application/json"})
    assert resp.status == 400
    data = await resp.json()
    assert "Invalid JSON" in data["error"]
