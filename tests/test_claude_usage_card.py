"""Tests for ClaudeUsageCard: _parse_screen, _hours_until_reset, parse_output, probe_command."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_rts.cards.claude_usage_card import ClaudeUsageCard, _hours_until_reset, _parse_screen
from tests.conftest import MockSession, MockSessionManager


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


# ── dialog flag tests (_puppet_probe trust_accepted / bypass_accepted) ─────────


class _MockPty:
    """Minimal PTY mock that records write() calls."""

    def __init__(self):
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)


class _StepScrollback:
    """Scrollback that returns a different bytes chunk on each get_all() call.

    Call advance() to move to the next step.  The bytes are accumulated so
    that the probe's consumed_size bookkeeping works correctly (it slices from
    the previous length).
    """

    def __init__(self, steps: list[bytes]):
        self._steps = steps
        self._idx = 0
        self._data = b""

    def advance(self) -> None:
        if self._idx < len(self._steps):
            self._data += self._steps[self._idx]
            self._idx += 1

    def get_all(self) -> bytes:
        return self._data


def _make_card_with_mock_session(scrollback: _StepScrollback, pty: _MockPty) -> tuple[ClaudeUsageCard, MockSession]:
    """Build a ClaudeUsageCard wired to a MockSession that has a real pty mock."""
    session = MockSession(alive=True, session_id="diag-session-01")
    session.scrollback = scrollback  # type: ignore[assignment]
    session.pty = pty  # type: ignore[attr-defined]
    mgr = MockSessionManager(session=session)
    card = ClaudeUsageCard("acct-alice", mgr)
    # shorten probe timeout so tests don't spin for the real default
    card._probe_timeout = 5.0
    return card, session


# Pre-encoded screens (plain ASCII — pyte renders them correctly).
_TRUST_SCREEN = b"Trust this folder?\r\nYes, I trust this folder\r\n"
_BYPASS_SCREEN = b"Permissions dialog\r\nYes, I accept  Bypass Permissions\r\n"
_WELCOME_SCREEN = b"Welcome back, alice!\r\n"
_USAGE_SCREEN = (
    b"  Session usage\r\n  50% used\r\n  Resets in 2h 30m\r\n  Weekly usage\r\n  30% used\r\n  Resets in 6 days\r\n"
)
_DEAD_SCREEN = b""  # session will be marked dead


@pytest.mark.asyncio
async def test_trust_dialog_sets_trust_accepted_and_sends_enter():
    """trust_accepted is set when trust-folder dialog is detected; \\r is sent."""
    pty = _MockPty()
    # Steps: trust dialog visible → session dies (so probe exits cleanly)
    scrollback = _StepScrollback([_TRUST_SCREEN])
    scrollback.advance()  # make trust screen immediately visible

    session = MockSession(alive=True, session_id="t1")
    session.scrollback = scrollback  # type: ignore[assignment]
    session.pty = pty  # type: ignore[attr-defined]

    # After one iteration that sends \r, kill the session so the loop exits
    original_write = pty.write

    def _write_and_kill(data: str) -> None:
        original_write(data)
        session.alive = False  # kill session after first dialog response

    pty.write = _write_and_kill  # type: ignore[method-assign]

    mgr = MockSessionManager(session=session)
    card = ClaudeUsageCard("acct-alice", mgr)
    card._probe_timeout = 5.0

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await card._puppet_probe(session=session)

    # The \r that accepts the trust dialog must have been written
    assert "\r" in pty.writes, f"Expected \\r in writes, got: {pty.writes!r}"


@pytest.mark.asyncio
async def test_bypass_dialog_sets_bypass_accepted_independently():
    """bypass_accepted is set independently from trust_accepted.

    With the old single-flag design, once trust was accepted the bypass dialog
    would be skipped.  With the split flags both dialogs trigger their own
    accept sequence.
    """
    _MockPty()
    write_log: list[str] = []

    # We'll control session death via a counter on writes
    call_count = [0]

    session = MockSession(alive=True, session_id="t2")

    def _dynamic_scrollback_factory():
        """Return a scrollback whose data evolves as writes arrive."""
        _data = [_TRUST_SCREEN]  # start with trust dialog

        class _DynamicScrollback:
            def get_all(self) -> bytes:
                return _data[0]

        sb = _DynamicScrollback()

        def _write(data: str) -> None:
            write_log.append(data)
            call_count[0] += 1
            if call_count[0] == 1:
                # First write (\r accepting trust) → advance to bypass dialog
                _data[0] = _TRUST_SCREEN + _BYPASS_SCREEN
            elif call_count[0] >= 3:
                # After bypass sequence (\x1b[B + \r) → kill session
                session.alive = False

        return sb, _write

    sb, write_fn = _dynamic_scrollback_factory()
    session.scrollback = sb  # type: ignore[assignment]
    session.pty = MagicMock()
    session.pty.write.side_effect = write_fn

    mgr = MockSessionManager(session=session)
    card = ClaudeUsageCard("acct-alice", mgr)
    card._probe_timeout = 5.0

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await card._puppet_probe(session=session)

    all_writes = [call.args[0] for call in session.pty.write.call_args_list]

    # Trust dialog: \r must appear
    assert "\r" in all_writes, f"No \\r for trust dialog. Writes: {all_writes!r}"
    # Bypass dialog: down-arrow + \r must appear
    assert "\x1b[B" in all_writes, f"No down-arrow for bypass dialog. Writes: {all_writes!r}"
    # Both dialogs were handled, so there must be at least 2 \r writes
    enter_count = all_writes.count("\r")
    assert enter_count >= 2, f"Expected >=2 \\r writes (trust + bypass), got {enter_count}. Writes: {all_writes!r}"


@pytest.mark.asyncio
async def test_trust_flag_prevents_double_accept():
    """trust_accepted flag ensures we do not send \\r twice for the same trust dialog."""
    pty_mock = MagicMock()
    write_log: list[str] = []
    call_count = [0]

    session = MockSession(alive=True, session_id="t3")

    # Scrollback that always shows the trust dialog (never advances)
    class _StaticScrollback:
        def get_all(self) -> bytes:
            return _TRUST_SCREEN

    session.scrollback = _StaticScrollback()  # type: ignore[assignment]

    def _write(data: str) -> None:
        write_log.append(data)
        call_count[0] += 1
        if call_count[0] >= 2:
            session.alive = False  # kill after a couple of writes

    pty_mock.write.side_effect = _write
    session.pty = pty_mock

    mgr = MockSessionManager(session=session)
    card = ClaudeUsageCard("acct-alice", mgr)
    card._probe_timeout = 5.0

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await card._puppet_probe(session=session)

    # Even though the trust screen never goes away, \r should be sent exactly once
    all_writes = [call.args[0] for call in pty_mock.write.call_args_list]
    enter_writes = [w for w in all_writes if w == "\r"]
    assert len(enter_writes) == 1, (
        f"Expected exactly 1 \\r for trust dialog (flag prevents repeat), got {len(enter_writes)}. "
        f"Writes: {all_writes!r}"
    )


@pytest.mark.asyncio
async def test_bypass_flag_prevents_double_accept():
    """bypass_accepted flag ensures we do not send the bypass sequence twice."""
    pty_mock = MagicMock()
    write_log: list[str] = []
    call_count = [0]

    session = MockSession(alive=True, session_id="t4")

    # Scrollback shows trust already done + bypass dialog persistently
    _combined = _TRUST_SCREEN + _BYPASS_SCREEN

    class _StaticScrollback:
        def get_all(self) -> bytes:
            return _combined

    session.scrollback = _StaticScrollback()  # type: ignore[assignment]

    def _write(data: str) -> None:
        write_log.append(data)
        call_count[0] += 1
        if call_count[0] >= 4:
            session.alive = False

    pty_mock.write.side_effect = _write
    session.pty = pty_mock

    mgr = MockSessionManager(session=session)
    card = ClaudeUsageCard("acct-alice", mgr)
    card._probe_timeout = 5.0

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await card._puppet_probe(session=session)

    all_writes = [call.args[0] for call in pty_mock.write.call_args_list]
    down_arrow_count = all_writes.count("\x1b[B")
    assert down_arrow_count == 1, (
        f"Expected exactly 1 down-arrow for bypass dialog (flag prevents repeat), "
        f"got {down_arrow_count}. Writes: {all_writes!r}"
    )
