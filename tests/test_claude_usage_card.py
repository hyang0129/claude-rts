"""Tests for ClaudeUsageCard: _parse_screen, _hours_until_reset, parse_output, probe_command."""

import pytest

from claude_rts.cards.claude_usage_card import ClaudeUsageCard, _hours_until_reset, _parse_screen
from tests.conftest import MockSessionManager


# ── _hours_until_reset ────────────────────────────────────────────────────────


def test_hours_until_reset_hours_and_minutes():
    assert _hours_until_reset("in 2h 14m") == pytest.approx(2 + 14 / 60)


def test_hours_until_reset_hours_only():
    assert _hours_until_reset("3h") == pytest.approx(3.0)


def test_hours_until_reset_minutes_only():
    assert _hours_until_reset("45m") == pytest.approx(0.75)


def test_hours_until_reset_empty_string():
    assert _hours_until_reset("") is None


def test_hours_until_reset_garbage():
    assert _hours_until_reset("garbage") is None


# ── _parse_screen ─────────────────────────────────────────────────────────────


_SCREEN_WITH_SESSION_AND_WEEK = """\
  Session usage
  50% used
  Resets in 2h 30m

  Weekly usage
  30% used
  Resets in 6 days
"""


def test_parse_screen_five_hour_pct():
    result = _parse_screen(_SCREEN_WITH_SESSION_AND_WEEK)
    assert result["five_hour_pct"] == 50.0


def test_parse_screen_seven_day_pct():
    result = _parse_screen(_SCREEN_WITH_SESSION_AND_WEEK)
    assert result["seven_day_pct"] == 30.0


def test_parse_screen_resets_extracted():
    result = _parse_screen(_SCREEN_WITH_SESSION_AND_WEEK)
    assert result["five_hour_resets"] is not None
    assert "2h" in result["five_hour_resets"]


def test_parse_screen_empty_text():
    result = _parse_screen("")
    assert result["five_hour_pct"] is None
    assert result["seven_day_pct"] is None


def test_parse_screen_no_pct():
    result = _parse_screen("No usage data here\nJust some text\n")
    assert result["five_hour_pct"] is None
    assert result["seven_day_pct"] is None


# ── parse_output ──────────────────────────────────────────────────────────────


def _make_card(identity: str = "acct-alice") -> ClaudeUsageCard:
    return ClaudeUsageCard(identity, MockSessionManager())


def test_parse_output_raises_when_no_data():
    card = _make_card()
    with pytest.raises(ValueError, match="No usage data"):
        card.parse_output("plain text with no percentages")


def test_parse_output_returns_dict_with_usage_data():
    card = _make_card("acct-alice")
    result = card.parse_output(_SCREEN_WITH_SESSION_AND_WEEK)
    assert result["five_hour_pct"] == 50.0
    assert result["seven_day_pct"] == 30.0


# ── probe_command ─────────────────────────────────────────────────────────────


def test_probe_command_contains_identity():
    card = _make_card("acct-alice")
    cmd = card.probe_command()
    assert "acct-alice" in cmd


def test_probe_command_uses_claude_config_dir():
    card = _make_card("acct-alice")
    cmd = card.probe_command()
    assert "CLAUDE_CONFIG_DIR=/profiles/acct-alice" in cmd


def test_probe_command_uses_dangerously_skip_permissions():
    card = _make_card("acct-alice")
    cmd = card.probe_command()
    assert "--dangerously-skip-permissions" in cmd


def test_probe_command_invalid_identity_raises():
    card = _make_card("acct-alice; rm -rf /")
    with pytest.raises(ValueError):
        card.probe_command()


def test_probe_command_uses_configured_container():
    card = ClaudeUsageCard("acct-alice", MockSessionManager(), container="my-util")
    cmd = card.probe_command()
    assert "my-util" in cmd
