"""Tests for claude_rts.profile_manager — pure functions and CredentialManager class."""

import math
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_rts.profile_manager import (
    CredentialManager,
    CredentialState,
    classify_burn,
    compute_burn_rate,
    parse_hours_until_reset,
)


# ── parse_hours_until_reset ──────────────────────────────────────────────────


def test_parse_hours_until_reset_hm_format():
    """'2h 14m' should parse to approximately 2.23 hours."""
    result = parse_hours_until_reset("2h 14m")
    assert result is not None
    assert abs(result - (2 + 14 / 60)) < 0.01


def test_parse_hours_until_reset_time_format():
    """'11pm (UTC)' should return some positive float (hours until next 11pm UTC)."""
    result = parse_hours_until_reset("11pm (UTC)")
    assert result is not None
    assert result > 0


def test_parse_hours_until_reset_none_input():
    """None input should return None."""
    result = parse_hours_until_reset(None)
    assert result is None


def test_parse_hours_until_reset_unparseable():
    """Garbage string that matches no pattern returns None."""
    result = parse_hours_until_reset("not a time at all")
    assert result is None


def test_parse_hours_until_reset_hm_format_prefix():
    """'in 1h 30m' prefix variant parses correctly."""
    result = parse_hours_until_reset("in 1h 30m")
    assert result is not None
    assert abs(result - 1.5) < 0.01


def test_parse_hours_until_reset_empty_string():
    """Empty string returns None."""
    result = parse_hours_until_reset("")
    assert result is None


# ── compute_burn_rate ────────────────────────────────────────────────────────


def test_compute_burn_rate_basic():
    """50% usage / 5 hours = 10.0/hr."""
    # Use hm format so parse_hours_until_reset returns exactly 5.0
    result = compute_burn_rate(50.0, "5h 0m")
    assert result is not None
    assert abs(result - 10.0) < 0.01


def test_compute_burn_rate_zero_hours():
    """When reset_str parses to <= 0 hours, return inf (past reset)."""
    # hours <= 0 path: patch parse_hours_until_reset to return 0
    with patch("claude_rts.profile_manager.parse_hours_until_reset", return_value=0):
        result = compute_burn_rate(50.0, "irrelevant")
    assert result == float("inf")


def test_compute_burn_rate_unparseable_reset():
    """None reset_str (unparseable) returns None."""
    result = compute_burn_rate(50.0, None)
    assert result is None


def test_compute_burn_rate_garbage_reset():
    """Garbage reset_str that cannot be parsed returns None."""
    result = compute_burn_rate(100.0, "not a time")
    assert result is None


# ── classify_burn ────────────────────────────────────────────────────────────


def test_classify_burn_overburning():
    """burn_rate=25 (> 20/hr nominal for 5hr window) → 'overburning'."""
    assert classify_burn(25.0) == "overburning"


def test_classify_burn_normal():
    """burn_rate=15 (between 10 and 20) → 'normal'."""
    assert classify_burn(15.0) == "normal"


def test_classify_burn_underburning():
    """burn_rate=5 (< 10, which is 50% of nominal 20/hr) → 'underburning'."""
    assert classify_burn(5.0) == "underburning"


def test_classify_burn_inf():
    """burn_rate=inf → 'overburning'."""
    assert classify_burn(float("inf")) == "overburning"


def test_classify_burn_exactly_nominal():
    """burn_rate exactly at nominal (20/hr) is not overburning — returns 'normal'."""
    # burn_rate > nominal is required for overburning; equal is not overburning
    result = classify_burn(20.0)
    assert result == "normal"


def test_classify_burn_exactly_half_nominal():
    """burn_rate exactly at 50% of nominal (10/hr) is not underburning — returns 'normal'."""
    # burn_rate < nominal * 0.5 required for underburning; equal is not underburning
    result = classify_burn(10.0)
    assert result == "normal"


# ── CredentialState.to_dict ──────────────────────────────────────────────────


def test_credential_state_to_dict_keys():
    """to_dict() includes all expected keys."""
    state = CredentialState(name="acct-alice")
    d = state.to_dict()
    expected_keys = {
        "name", "usage_5hr_pct", "usage_daily_pct", "five_hour_resets",
        "seven_day_resets", "burn_rate", "burn_class", "health",
        "account_id", "last_probe_time", "last_probe_wall", "data_timestamp",
        "last_health_check", "error",
    }
    assert expected_keys == set(d.keys())
    assert d["name"] == "acct-alice"


# ── CredentialManager unit tests ─────────────────────────────────────────────

_UTIL_MOD = "claude_rts.profile_manager"


def _make_manager() -> CredentialManager:
    """Return a CredentialManager with no background tasks started."""
    return CredentialManager(MagicMock(), probe_interval=60, health_check_interval=900)


def test_get_all_empty():
    """Empty cache returns empty list."""
    mgr = _make_manager()
    assert mgr.get_all() == []


def test_get_all_sorted_by_burn_rate():
    """get_all() returns credentials sorted by burn rate, highest first."""
    mgr = _make_manager()
    mgr._cache["low"] = CredentialState(name="low", burn_rate=5.0)
    mgr._cache["high"] = CredentialState(name="high", burn_rate=25.0)
    mgr._cache["mid"] = CredentialState(name="mid", burn_rate=15.0)

    result = mgr.get_all()
    names = [s.name for s in result]
    assert names == ["high", "mid", "low"]


def test_get_all_none_burn_rate_last():
    """Credentials with None burn_rate sort after those with real rates."""
    mgr = _make_manager()
    mgr._cache["real"] = CredentialState(name="real", burn_rate=10.0)
    mgr._cache["none"] = CredentialState(name="none", burn_rate=None)

    result = mgr.get_all()
    assert result[0].name == "real"
    assert result[-1].name == "none"


def test_get_best_returns_lowest_burn_healthy():
    """get_best() picks the healthy credential with the lowest burn rate."""
    mgr = _make_manager()
    mgr._first_probe_done = True
    mgr._cache["high"] = CredentialState(name="high", burn_rate=20.0, health="healthy")
    mgr._cache["low"] = CredentialState(name="low", burn_rate=5.0, health="healthy")
    mgr._cache["mid"] = CredentialState(name="mid", burn_rate=12.0, health="healthy")

    best = mgr.get_best()
    assert best is not None
    assert best.name == "low"


def test_get_best_excludes_stale():
    """Stale credentials are excluded from get_best()."""
    mgr = _make_manager()
    mgr._first_probe_done = True
    mgr._cache["stale"] = CredentialState(name="stale", burn_rate=1.0, health="stale")
    mgr._cache["healthy"] = CredentialState(name="healthy", burn_rate=15.0, health="healthy")

    best = mgr.get_best()
    assert best is not None
    assert best.name == "healthy"


def test_get_best_empty_cache():
    """Returns None when no credentials are cached."""
    mgr = _make_manager()
    mgr._first_probe_done = True
    assert mgr.get_best() is None


def test_get_best_cache_not_ready():
    """Returns None before the first probe cycle completes."""
    mgr = _make_manager()
    mgr._cache["acct-x"] = CredentialState(name="acct-x", burn_rate=5.0, health="healthy")
    # _first_probe_done defaults to False
    assert mgr.get_best() is None


def test_is_cache_ready_false_initially():
    """Cache is not ready until the first probe cycle completes."""
    mgr = _make_manager()
    assert mgr.is_cache_ready() is False


def test_is_cache_ready_true_after_flag():
    """is_cache_ready() returns True once _first_probe_done is set."""
    mgr = _make_manager()
    mgr._first_probe_done = True
    assert mgr.is_cache_ready() is True


async def test_force_probe_updates_cache():
    """force_probe() calls _probe_one and stores the result in the cache."""
    mgr = _make_manager()

    updated_state = CredentialState(
        name="acct-test",
        usage_5hr_pct=42.0,
        burn_rate=8.4,
        burn_class="normal",
        health="unknown",
    )

    with patch.object(mgr, "_probe_one", new=AsyncMock(return_value=updated_state)):
        state = await mgr.force_probe("acct-test")

    assert state is updated_state
    assert mgr._cache["acct-test"] is updated_state


async def test_force_health_check_marks_healthy():
    """force_health_check() sets health='healthy' when health_check_profile returns True."""
    mgr = _make_manager()
    mgr._cache["acct-good"] = CredentialState(name="acct-good")

    with patch(f"{_UTIL_MOD}.health_check_profile", new=AsyncMock(return_value=True)):
        state = await mgr.force_health_check("acct-good")

    assert state.health == "healthy"
    assert state.last_health_check is not None


async def test_force_health_check_marks_stale():
    """force_health_check() sets health='stale' when health_check_profile returns False."""
    mgr = _make_manager()
    mgr._cache["acct-bad"] = CredentialState(name="acct-bad")

    with patch(f"{_UTIL_MOD}.health_check_profile", new=AsyncMock(return_value=False)):
        state = await mgr.force_health_check("acct-bad")

    assert state.health == "stale"


async def test_force_health_check_creates_state_if_missing():
    """force_health_check() creates a new CredentialState if the profile is not in cache."""
    mgr = _make_manager()

    with patch(f"{_UTIL_MOD}.health_check_profile", new=AsyncMock(return_value=True)):
        state = await mgr.force_health_check("acct-new")

    assert "acct-new" in mgr._cache
    assert state.health == "healthy"


async def test_create_profile_success():
    """create_profile() calls create_profile_dir and initializes cache entry on success."""
    mgr = _make_manager()

    with patch(f"{_UTIL_MOD}.create_profile_dir", new=AsyncMock(return_value=True)):
        result = await mgr.create_profile("acct-new")

    assert result["success"] is True
    assert result["name"] == "acct-new"
    assert "acct-new" in mgr._cache
    assert mgr._cache["acct-new"].health == "unknown"


async def test_create_profile_failure():
    """create_profile() returns success=False when create_profile_dir fails."""
    mgr = _make_manager()

    with patch(f"{_UTIL_MOD}.create_profile_dir", new=AsyncMock(return_value=False)):
        result = await mgr.create_profile("acct-bad")

    assert result["success"] is False
    assert "error" in result
    assert "acct-bad" not in mgr._cache


async def test_delete_profile_removes_from_cache():
    """delete_profile() calls delete_profile_dir and evicts the entry from cache."""
    mgr = _make_manager()
    mgr._cache["acct-gone"] = CredentialState(name="acct-gone")

    with patch(f"{_UTIL_MOD}.delete_profile_dir", new=AsyncMock(return_value=True)):
        success = await mgr.delete_profile("acct-gone")

    assert success is True
    assert "acct-gone" not in mgr._cache


async def test_delete_profile_not_in_cache_still_calls_dir_delete():
    """delete_profile() calls delete_profile_dir even if the profile is not in cache."""
    mgr = _make_manager()

    with patch(f"{_UTIL_MOD}.delete_profile_dir", new=AsyncMock(return_value=True)) as mock_del:
        success = await mgr.delete_profile("acct-missing")

    assert success is True
    mock_del.assert_awaited_once_with("acct-missing")


async def test_delete_profile_failure_leaves_cache_intact():
    """delete_profile() does NOT evict cache entry when delete_profile_dir returns False."""
    mgr = _make_manager()
    state = CredentialState(name="acct-keep")
    mgr._cache["acct-keep"] = state

    with patch(f"{_UTIL_MOD}.delete_profile_dir", new=AsyncMock(return_value=False)):
        success = await mgr.delete_profile("acct-keep")

    assert success is False
    assert "acct-keep" in mgr._cache


async def test_probe_one_happy_path():
    """_probe_one() returns a populated CredentialState on a successful probe."""
    mgr = _make_manager()

    mock_usage = {
        "five_hour_pct": 50.0,
        "seven_day_pct": 30.0,
        "five_hour_resets": "2h 30m",
        "seven_day_resets": "Apr 7, 3pm (UTC)",
    }

    with patch(f"{_UTIL_MOD}.probe_usage_via_session", new=AsyncMock(return_value=mock_usage)), \
         patch(f"{_UTIL_MOD}.read_account_id_file", new=AsyncMock(return_value="acct-123")), \
         patch(f"{_UTIL_MOD}.get_account_id", new=AsyncMock(return_value=None)), \
         patch(f"{_UTIL_MOD}.write_account_id_file", new=AsyncMock()):
        state = await mgr._probe_one("acct-alice")

    assert state.name == "acct-alice"
    assert state.usage_5hr_pct == 50.0
    assert state.burn_rate is not None
    assert state.burn_rate > 0
    assert state.burn_class in ("overburning", "normal", "underburning")
    assert state.account_id == "acct-123"
    assert state.last_probe_time is not None
    assert state.error is None


async def test_probe_one_probe_returns_none():
    """_probe_one() handles probe_usage_via_session returning None gracefully."""
    mgr = _make_manager()

    with patch(f"{_UTIL_MOD}.probe_usage_via_session", new=AsyncMock(return_value=None)):
        state = await mgr._probe_one("acct-fail")

    assert state.name == "acct-fail"
    assert state.error == "usage.json not found"
    assert state.last_probe_time is not None


async def test_probe_one_probe_raises_exception():
    """_probe_one() handles exceptions from probe_usage_via_session without crashing."""
    mgr = _make_manager()

    with patch(f"{_UTIL_MOD}.probe_usage_via_session", new=AsyncMock(side_effect=RuntimeError("container down"))):
        state = await mgr._probe_one("acct-err")

    assert state.name == "acct-err"
    assert "container down" in state.error
    assert state.last_probe_time is not None
