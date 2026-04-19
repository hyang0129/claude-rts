"""Tests for the /api/profiles endpoints (issue #72, updated #163).

Tests the profile manager API:
- GET /api/profiles lists probe profiles with latest usage data
- GET /api/profiles/main returns the current main profile slot name + existence
- PUT /api/profiles/main copies credentials from a source profile into the main slot
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


async def test_profiles_list_includes_main_profile_name(client, tmp_path):
    """Each profile entry includes main_profile_name and is_main.

    ``is_main`` is True for exactly the profile that matches active_main_source
    in config, and False for all others.  When active_main_source is absent
    every entry has is_main=False.
    """
    app_config = client.app["app_config"]
    cfg = config.read_config(app_config)
    cfg["probe_profiles"] = ["alice", "bob"]
    cfg["active_main_source"] = "alice"
    config.write_config(app_config, cfg)

    with patch("claude_rts.server.ServiceCardRegistry.get", return_value=None):
        resp = await client.get("/api/profiles")

    data = await resp.json()
    assert resp.status == 200
    assert len(data) == 2
    # main_profile_name is still broadcast on every entry
    assert all(p["main_profile_name"] == "main" for p in data)
    # is_main must be present and reflect active_main_source
    by_profile = {p["profile"]: p for p in data}
    assert by_profile["alice"]["is_main"] is True
    assert by_profile["bob"]["is_main"] is False

    # When active_main_source is absent, all is_main values must be False.
    cfg2 = config.read_config(app_config)
    cfg2.pop("active_main_source", None)
    config.write_config(app_config, cfg2)

    with patch("claude_rts.server.ServiceCardRegistry.get", return_value=None):
        resp2 = await client.get("/api/profiles")

    data2 = await resp2.json()
    assert all(p["is_main"] is False for p in data2)


# ── GET /api/profiles/main ───────────────────────────────────────────────────


async def test_main_profile_get_default(client):
    """Fresh config returns the default main_profile_name 'main' with exists=False."""
    with patch("claude_rts.server.exec_in_util", new_callable=AsyncMock, return_value=(1, "")):
        resp = await client.get("/api/profiles/main")
    assert resp.status == 200
    data = await resp.json()
    assert data["main_profile_name"] == "main"
    assert data["exists"] is False


async def test_main_profile_get_reports_exists_true(client):
    """When the credentials file is present in the util container, exists=True."""
    with patch("claude_rts.server.exec_in_util", new_callable=AsyncMock, return_value=(0, "")):
        resp = await client.get("/api/profiles/main")
    data = await resp.json()
    assert data["exists"] is True


async def test_main_profile_get_custom_name(client):
    """main_profile_name override in config is reflected in the response."""
    app_config = client.app["app_config"]
    cfg = config.read_config(app_config)
    cfg["main_profile_name"] = "custom-slot"
    config.write_config(app_config, cfg)

    with patch("claude_rts.server.exec_in_util", new_callable=AsyncMock, return_value=(1, "")):
        resp = await client.get("/api/profiles/main")
    data = await resp.json()
    assert data["main_profile_name"] == "custom-slot"


# ── PUT /api/profiles/main ───────────────────────────────────────────────────


async def test_main_profile_set_copies_credentials(client):
    """PUT with a valid source profile invokes the util-container credential copy."""
    app_config = client.app["app_config"]
    cfg = config.read_config(app_config)
    cfg["probe_profiles"] = ["hongy"]
    config.write_config(app_config, cfg)

    mock_exec = AsyncMock(return_value=(0, ""))
    with patch("claude_rts.server.exec_in_util", mock_exec):
        resp = await client.put(
            "/api/profiles/main",
            json={"source_profile": "hongy"},
        )
    assert resp.status == 200
    data = await resp.json()
    assert data["main_profile_name"] == "main"
    assert data["source_profile"] == "hongy"
    assert data["status"] == "ok"

    # Verify the copy command references the correct source and destination.
    assert mock_exec.call_count == 1
    sent_cmd = mock_exec.call_args[0][1]
    assert "/profiles/hongy/.credentials.json" in sent_cmd
    assert "/profiles/main/.credentials.json" in sent_cmd
    # .claude.json must also be copied (best-effort, tolerating a missing source).
    assert "/profiles/hongy/.claude.json" in sent_cmd
    assert "/profiles/main/.claude.json" in sent_cmd
    assert "|| true" in sent_cmd


async def test_main_profile_set_unknown_source_returns_400(client):
    """Setting main to a profile not in probe_profiles/discovered returns 400."""
    resp = await client.put(
        "/api/profiles/main",
        json={"source_profile": "nonexistent"},
    )
    assert resp.status == 400


async def test_main_profile_set_missing_source_returns_400(client):
    """PUT without source_profile returns 400."""
    resp = await client.put("/api/profiles/main", json={})
    assert resp.status == 400


async def test_main_profile_set_cannot_promote_into_itself(client):
    """Attempting to promote the main slot into itself returns 400."""
    app_config = client.app["app_config"]
    cfg = config.read_config(app_config)
    cfg["probe_profiles"] = ["main"]  # pathological: main in tracked list
    cfg["main_profile_name"] = "main"
    config.write_config(app_config, cfg)

    resp = await client.put(
        "/api/profiles/main",
        json={"source_profile": "main"},
    )
    assert resp.status == 400


async def test_main_profile_get_rejects_unsafe_config_name(client):
    """If config.json has a main_profile_name with shell metacharacters, the
    handler must refuse to interpolate it (500) rather than shell-inject."""
    app_config = client.app["app_config"]
    cfg = config.read_config(app_config)
    cfg["main_profile_name"] = "foo; rm -rf /"
    config.write_config(app_config, cfg)

    resp = await client.get("/api/profiles/main")
    assert resp.status == 500


async def test_main_profile_set_rejects_unsafe_source_name(client):
    """PUT with a shell-metacharacter source_profile is rejected (400) before
    any shell command is built."""
    resp = await client.put(
        "/api/profiles/main",
        json={"source_profile": "foo; rm -rf /"},
    )
    assert resp.status == 400


async def test_main_profile_set_copy_failure_returns_500(client):
    """When the in-container copy command fails, the server returns 500."""
    app_config = client.app["app_config"]
    cfg = config.read_config(app_config)
    cfg["probe_profiles"] = ["hongy"]
    config.write_config(app_config, cfg)

    with patch(
        "claude_rts.server.exec_in_util",
        new_callable=AsyncMock,
        return_value=(1, "cp: no such file"),
    ):
        resp = await client.put(
            "/api/profiles/main",
            json={"source_profile": "hongy"},
        )
    assert resp.status == 500


# ── Route registration ───────────────────────────────────────────────────────


async def test_app_has_profiles_routes(app):
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/api/profiles" in routes
    assert "/api/profiles/main" in routes
