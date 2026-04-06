"""Tests for the cred_store module."""

import pytest
from unittest.mock import patch

from claude_rts.cred_store import (
    read_credentials,
    write_credentials,
    mask_key,
    add_credential,
    delete_credential,
    set_priority,
    get_priority,
    update_credential_usage,
    list_credentials_ranked,
)


@pytest.fixture
def creds_file(tmp_path):
    cred_path = tmp_path / "credentials.json"
    with patch("claude_rts.cred_store.CREDENTIALS_FILE", cred_path):
        yield cred_path


# ── read_credentials ────────────────────────────────────────────────────────


def test_read_credentials_default_when_missing(creds_file):
    """Returns default structure when file doesn't exist."""
    data = read_credentials()
    assert data == {"priority": None, "credentials": []}


def test_write_and_read_round_trip(creds_file):
    """Write then read back produces identical data."""
    payload = {"priority": "cred-abc", "credentials": [{"id": "cred-abc", "label": "test"}]}
    write_credentials(payload)
    result = read_credentials()
    assert result == payload


# ── mask_key ────────────────────────────────────────────────────────────────


def test_mask_key_long(creds_file):
    """Long key returns first 7 chars + '...' + last 4 chars."""
    key = "sk-ant-api03-ABCDEFGHIJK1234"
    result = mask_key(key)
    assert result == key[:7] + "..." + key[-4:]


def test_mask_key_short(creds_file):
    """Short key (<=11 chars) returns a masked string without original content."""
    key = "shortkey"
    result = mask_key(key)
    assert key not in result
    assert result != ""


def test_mask_key_empty(creds_file):
    """mask_key('') returns ''."""
    assert mask_key("") == ""


def test_mask_key_none(creds_file):
    """mask_key(None) returns ''."""
    assert mask_key(None) == ""


# ── add_credential ──────────────────────────────────────────────────────────


def test_add_credential_returns_no_raw_key(creds_file):
    """add_credential return value does not expose the raw key field."""
    result = add_credential("My Label", "sk-ant-api03-supersecretkeyvalue")
    assert "key" not in result


def test_add_credential_has_key_hint(creds_file):
    """add_credential return value contains key_hint field."""
    result = add_credential("My Label", "sk-ant-api03-supersecretkeyvalue")
    assert "key_hint" in result
    assert result["key_hint"] != ""


def test_add_credential_raises_on_empty_label(creds_file):
    """add_credential raises ValueError for empty label."""
    with pytest.raises(ValueError, match="label"):
        add_credential("", "sk-ant-api03-somevalidkey")


def test_add_credential_raises_on_empty_key(creds_file):
    """add_credential raises ValueError for empty key."""
    with pytest.raises(ValueError, match="key"):
        add_credential("Valid Label", "")


# ── list_credentials_ranked ─────────────────────────────────────────────────


def test_list_credentials_ranked_empty(creds_file):
    """Returns [] when no credentials exist."""
    result = list_credentials_ranked()
    assert result == []


def test_list_credentials_sorted_by_burn_rate(creds_file):
    """Credentials are sorted ascending by burn_rate."""
    c1 = add_credential("High Burn", "sk-ant-api03-highburnkeyvalue1234")
    c2 = add_credential("Low Burn", "sk-ant-api03-lowburnkeyvalue12345")
    c3 = add_credential("Mid Burn", "sk-ant-api03-midburnkeyvalue12345")

    update_credential_usage(c1["id"], {"burn_rate": 80.0})
    update_credential_usage(c2["id"], {"burn_rate": 10.0})
    update_credential_usage(c3["id"], {"burn_rate": 45.0})

    ranked = list_credentials_ranked()
    burn_rates = [r["burn_rate"] for r in ranked]
    assert burn_rates == sorted(burn_rates)


def test_list_credentials_none_burn_rate_last(creds_file):
    """Credentials with None burn_rate sort after those with a value."""
    c_none = add_credential("No Burn", "sk-ant-api03-noburnkeyvalue123456")
    c_val = add_credential("Has Burn", "sk-ant-api03-hasburnkeyvalue12345")

    update_credential_usage(c_val["id"], {"burn_rate": 5.0})
    # c_none retains burn_rate=None

    ranked = list_credentials_ranked()
    ids = [r["id"] for r in ranked]
    assert ids.index(c_val["id"]) < ids.index(c_none["id"])


def test_list_credentials_no_raw_key(creds_file):
    """No returned record contains the raw 'key' field."""
    add_credential("Cred A", "sk-ant-api03-credAkeyvalue123456")
    add_credential("Cred B", "sk-ant-api03-credBkeyvalue123456")
    for cred in list_credentials_ranked():
        assert "key" not in cred


def test_list_credentials_has_is_priority(creds_file):
    """Each returned record has an 'is_priority' boolean field."""
    add_credential("Cred X", "sk-ant-api03-credXkeyvalue123456")
    for cred in list_credentials_ranked():
        assert "is_priority" in cred
        assert isinstance(cred["is_priority"], bool)


# ── delete_credential ───────────────────────────────────────────────────────


def test_delete_credential_existing(creds_file):
    """delete_credential returns True when the id exists."""
    cred = add_credential("Delete Me", "sk-ant-api03-deletemekeyvalue123")
    assert delete_credential(cred["id"]) is True


def test_delete_credential_nonexistent(creds_file):
    """delete_credential returns False for an unknown id."""
    assert delete_credential("cred-doesnotexist") is False


def test_delete_credential_clears_priority(creds_file):
    """Deleting the priority credential clears the priority field."""
    cred = add_credential("Priority One", "sk-ant-api03-priorityonekeyval1")
    set_priority(cred["id"])
    assert get_priority() is not None

    delete_credential(cred["id"])
    assert get_priority() is None


# ── set_priority / get_priority ─────────────────────────────────────────────


def test_set_priority_existing(creds_file):
    """set_priority returns True and get_priority returns that credential."""
    cred = add_credential("Priority Cred", "sk-ant-api03-prioritycredkeyval")
    result = set_priority(cred["id"])
    assert result is True
    priority = get_priority()
    assert priority is not None
    assert priority["id"] == cred["id"]


def test_set_priority_nonexistent(creds_file):
    """set_priority returns False for an unknown id."""
    assert set_priority("cred-fakeid") is False


def test_get_priority_none_initially(creds_file):
    """get_priority returns None when no priority has been set."""
    assert get_priority() is None


def test_get_priority_no_raw_key(creds_file):
    """Priority credential returned by get_priority has no raw 'key' field."""
    cred = add_credential("Key Owner", "sk-ant-api03-keyownerkeyvalue123")
    set_priority(cred["id"])
    priority = get_priority()
    assert priority is not None
    assert "key" not in priority


# ── update_credential_usage ─────────────────────────────────────────────────


def test_update_credential_usage_existing(creds_file):
    """update_credential_usage returns True and fields appear in list output."""
    cred = add_credential("Usage Cred", "sk-ant-api03-usagecredkeyvalue1")
    result = update_credential_usage(cred["id"], {"burn_rate": 42.5, "usage_7d": 100})
    assert result is True

    ranked = list_credentials_ranked()
    match = next(r for r in ranked if r["id"] == cred["id"])
    assert match["burn_rate"] == 42.5
    assert match["usage_7d"] == 100


def test_update_credential_usage_nonexistent(creds_file):
    """update_credential_usage returns False for an unknown id."""
    assert update_credential_usage("cred-ghost", {"burn_rate": 10.0}) is False
