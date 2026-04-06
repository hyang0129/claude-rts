"""Tests for ClaudeUsageCard: _hours_until_reset and parse_output logic."""

import json
import pytest

from claude_rts.cards.claude_usage_card import ClaudeUsageCard, _hours_until_reset
from tests.conftest import MockSessionManager


# ── _hours_until_reset ────────────────────────────────────────────────────────


def test_hours_until_reset_hours_and_minutes():
    assert _hours_until_reset("2h 14m") == pytest.approx(2 + 14 / 60)


def test_hours_until_reset_hours_only():
    assert _hours_until_reset("3h") == pytest.approx(3.0)


def test_hours_until_reset_minutes_only():
    assert _hours_until_reset("45m") == pytest.approx(0.75)


def test_hours_until_reset_empty_string():
    assert _hours_until_reset("") is None


def test_hours_until_reset_garbage():
    assert _hours_until_reset("garbage") is None


# ── parse_output helpers ──────────────────────────────────────────────────────

_VALID_JSON = {
    "five_hour_pct": 50,
    "five_hour_resets": "2h",
    "seven_day_pct": 30,
    "seven_day_resets": "in 6 days",
}


def _make_card(identity: str = "acct-alice") -> ClaudeUsageCard:
    return ClaudeUsageCard(identity, MockSessionManager())


def _wrap_ansi(text: str) -> str:
    """Wrap text in ANSI escape codes to simulate real claude-usage output."""
    return f"\x1b[32m{text}\x1b[0m"


# ── parse_output: valid JSON with ANSI codes ──────────────────────────────────


def test_parse_output_strips_ansi_and_parses():
    card = _make_card()
    raw_json = json.dumps(_VALID_JSON)
    output = _wrap_ansi(raw_json)

    result = card.parse_output(output)

    assert result["profile"] == "acct-alice"
    assert result["five_hour_pct"] == 50
    assert result["five_hour_resets"] == "2h"
    assert result["seven_day_pct"] == 30
    assert result["seven_day_resets"] == "in 6 days"


# ── parse_output: login prompt → ValueError ───────────────────────────────────


def test_parse_output_login_prompt_raises():
    card = _make_card()
    output = "Select login method\n  1. Claude account\n  2. API key\n"
    with pytest.raises(ValueError, match="not authenticated"):
        card.parse_output(output)


# ── parse_output: seven_day_resets is null → ValueError ──────────────────────


def test_parse_output_null_seven_day_resets_raises():
    card = _make_card()
    data = {**_VALID_JSON, "seven_day_resets": None}
    with pytest.raises(ValueError, match="seven_day_resets is null"):
        card.parse_output(json.dumps(data))


# ── parse_output: no JSON in output → ValueError ─────────────────────────────


def test_parse_output_no_json_raises():
    card = _make_card()
    with pytest.raises(ValueError, match="No JSON"):
        card.parse_output("some plain text with no braces")


# ── parse_output: burn_rate computed correctly ────────────────────────────────


def test_parse_output_burn_rate_computed():
    card = _make_card()
    data = {**_VALID_JSON, "five_hour_pct": 50, "five_hour_resets": "2h"}
    result = card.parse_output(json.dumps(data))

    # burn_rate = five_hour_pct / hours_until_reset = 50 / 2.0 = 25.0
    assert result["burn_rate"] == pytest.approx(25.0)


# ── probe_command: valid identity ────────────────────────────────────────────


def test_probe_command_valid_identity():
    card = _make_card("acct-alice")
    cmd = card.probe_command()
    assert "acct-alice" in cmd
    assert "--json" in cmd
    assert "claude-usage" in cmd


# ── probe_command: invalid identity with semicolon → ValueError ───────────────


def test_probe_command_invalid_identity_raises():
    card = _make_card("acct-alice; rm -rf /")
    with pytest.raises(ValueError, match="invalid characters"):
        card.probe_command()
