"""API-level tests for credential endpoints in server.py."""

from unittest.mock import patch

import pytest

from claude_rts.server import create_app


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture
def cred_dir(tmp_path):
    """Patch CREDENTIALS_FILE and CONFIG_DIR to use tmp_path."""
    cfg_dir = tmp_path / ".supreme-claudemander"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    creds_file = cfg_dir / "credentials.json"
    with (
        patch("claude_rts.cred_store.CREDENTIALS_FILE", creds_file),
        patch("claude_rts.config.CONFIG_DIR", cfg_dir),
    ):
        yield cfg_dir, creds_file


@pytest.fixture
def app(cred_dir):
    return create_app(test_mode=True)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


# ── Tests ──────────────────────────────────────────────────


async def test_get_credentials_empty(client, cred_dir):
    """GET /api/credentials returns empty list when no credentials stored."""
    resp = await client.get("/api/credentials")
    assert resp.status == 200
    data = await resp.json()
    assert data["priority"] is None
    assert data["credentials"] == []


async def test_post_credentials_adds_credential(client, cred_dir):
    """POST /api/credentials with valid data returns 201 with masked credential."""
    resp = await client.post(
        "/api/credentials",
        json={"label": "Test", "key": "sk-ant-api03-abcdefghijklmnop"},
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["label"] == "Test"
    assert "key_hint" in data
    assert "key" not in data
    assert "id" in data


async def test_post_credentials_missing_fields(client, cred_dir):
    """POST /api/credentials with missing 'key' field returns 400."""
    resp = await client.post(
        "/api/credentials",
        json={"label": "No Key Here"},
    )
    assert resp.status == 400


async def test_post_credentials_empty_label(client, cred_dir):
    """POST /api/credentials with empty label returns 400."""
    resp = await client.post(
        "/api/credentials",
        json={"label": "", "key": "sk-ant-api03-abcdefghijklmnop"},
    )
    assert resp.status == 400


async def test_delete_credential_existing(client, cred_dir):
    """Add then delete a credential — verify 200 with {'deleted': <id>}."""
    add_resp = await client.post(
        "/api/credentials",
        json={"label": "ToDelete", "key": "sk-ant-api03-deleteme1234567"},
    )
    assert add_resp.status == 201
    cred = await add_resp.json()
    cred_id = cred["id"]

    del_resp = await client.delete(f"/api/credentials/{cred_id}")
    assert del_resp.status == 200
    del_data = await del_resp.json()
    assert del_data["deleted"] == cred_id


async def test_delete_credential_nonexistent(client, cred_dir):
    """DELETE /api/credentials/not-real-id returns 404."""
    resp = await client.delete("/api/credentials/not-real-id")
    assert resp.status == 404


async def test_get_priority_no_priority_set(client, cred_dir):
    """GET /api/credentials/priority returns 404 when no priority is set."""
    resp = await client.get("/api/credentials/priority")
    assert resp.status == 404


async def test_put_and_get_priority(client, cred_dir):
    """Add a credential, PUT priority with its id, then GET priority returns 200."""
    add_resp = await client.post(
        "/api/credentials",
        json={"label": "PriorityOne", "key": "sk-ant-api03-prioritykey12345"},
    )
    assert add_resp.status == 201
    cred = await add_resp.json()
    cred_id = cred["id"]

    put_resp = await client.put(
        "/api/credentials/priority",
        json={"id": cred_id},
    )
    assert put_resp.status == 200
    put_data = await put_resp.json()
    assert put_data["priority"] == cred_id

    get_resp = await client.get("/api/credentials/priority")
    assert get_resp.status == 200
    get_data = await get_resp.json()
    assert get_data["id"] == cred_id
    assert get_data["label"] == "PriorityOne"


async def test_put_priority_nonexistent_id(client, cred_dir):
    """PUT /api/credentials/priority with fake id returns 404."""
    resp = await client.put(
        "/api/credentials/priority",
        json={"id": "fake-id"},
    )
    assert resp.status == 404


async def test_put_priority_missing_id_field(client, cred_dir):
    """PUT /api/credentials/priority with empty body returns 400."""
    resp = await client.put(
        "/api/credentials/priority",
        json={},
    )
    assert resp.status == 400


async def test_credentials_never_expose_raw_key(client, cred_dir):
    """POST a credential then GET /api/credentials — raw key must not appear."""
    raw_key = "sk-ant-api03-supersecretkey99999"
    add_resp = await client.post(
        "/api/credentials",
        json={"label": "SecretTest", "key": raw_key},
    )
    assert add_resp.status == 201

    get_resp = await client.get("/api/credentials")
    assert get_resp.status == 200
    body_text = await get_resp.text()
    assert raw_key not in body_text


async def test_credentials_persist_across_requests(client, cred_dir):
    """Add a credential via POST then GET /api/credentials — it appears in list."""
    add_resp = await client.post(
        "/api/credentials",
        json={"label": "Persistent", "key": "sk-ant-api03-persistentkey12345"},
    )
    assert add_resp.status == 201
    added = await add_resp.json()

    get_resp = await client.get("/api/credentials")
    assert get_resp.status == 200
    data = await get_resp.json()
    ids = [c["id"] for c in data["credentials"]]
    assert added["id"] in ids


async def test_update_usage_existing(client, cred_dir):
    """Add credential, PUT /api/credentials/{id}/usage with burn_rate, verify 200."""
    add_resp = await client.post(
        "/api/credentials",
        json={"label": "UsageTest", "key": "sk-ant-api03-usagetestkey12345"},
    )
    assert add_resp.status == 201
    cred = await add_resp.json()
    cred_id = cred["id"]

    usage_resp = await client.put(
        f"/api/credentials/{cred_id}/usage",
        json={"burn_rate": 5.5},
    )
    assert usage_resp.status == 200
    usage_data = await usage_resp.json()
    assert usage_data["updated"] == cred_id


async def test_update_usage_nonexistent(client, cred_dir):
    """PUT /api/credentials/fake-id/usage with burn_rate returns 404."""
    resp = await client.put(
        "/api/credentials/fake-id/usage",
        json={"burn_rate": 5.5},
    )
    assert resp.status == 404
