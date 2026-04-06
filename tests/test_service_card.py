"""Tests for ServiceCard: run_probe, subscriber notifications, start/stop lifecycle."""

import asyncio

from claude_rts.cards.service_card import ServiceCard
from tests.conftest import ProbeCard, MockSession, MockSessionManager


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_run_probe_success():
    """run_probe returns parsed dict and stores it in last_result."""
    session = MockSession(data=b"probe output\n", alive=False)
    mgr = MockSessionManager(session)
    card = ProbeCard("test-identity", mgr, probe_timeout=5.0)

    result = await card.run_probe()

    assert result == {"raw": "probe output\n", "parsed": True}
    assert card.last_result == result


async def test_run_probe_notifies_subscriber():
    """Sync subscriber callback receives the result dict after run_probe."""
    session = MockSession(data=b"output data\n", alive=False)
    mgr = MockSessionManager(session)
    card = ProbeCard("test-identity", mgr, probe_timeout=5.0)

    received: list[dict] = []
    card.subscribe(lambda r: received.append(r))

    result = await card.run_probe()

    assert len(received) == 1
    assert received[0] == result


async def test_run_probe_async_subscriber():
    """Async subscriber callback is eventually invoked after run_probe."""
    session = MockSession(data=b"async output\n", alive=False)
    mgr = MockSessionManager(session)
    card = ProbeCard("test-identity", mgr, probe_timeout=5.0)

    received: list[dict] = []

    async def async_callback(r: dict) -> None:
        received.append(r)

    card.subscribe(async_callback)

    result = await card.run_probe()

    # Give the event loop time to execute the fire-and-forget task.
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0] == result


async def test_run_probe_parse_failure():
    """run_probe returns None and does not update last_result when parse_output raises."""

    class FailingCard(ServiceCard):
        card_type = "failing"

        def probe_command(self) -> str:
            return "echo fail"

        def parse_output(self, output: str) -> dict:
            raise ValueError("bad output")

    session = MockSession(data=b"garbage\n", alive=False)
    mgr = MockSessionManager(session)
    card = FailingCard("test-identity", mgr, probe_timeout=5.0)

    result = await card.run_probe()

    assert result is None
    assert card.last_result is None


async def test_run_probe_timeout():
    """run_probe returns None on timeout and calls destroy_session."""
    # alive=True means the polling loop will never exit normally.
    session = MockSession(data=b"", alive=True)
    mgr = MockSessionManager(session)
    card = ProbeCard("test-identity", mgr, probe_timeout=0.3)

    result = await card.run_probe()

    assert result is None
    assert card.last_result is None
    # destroy_session must be called once to clean up the timed-out session.
    assert mgr.destroyed == ["mock-session-01"]


async def test_subscribe_unsubscribe():
    """subscriber_count tracks additions/removals; only remaining subscriber is notified."""
    session = MockSession(data=b"hello\n", alive=False)
    mgr = MockSessionManager(session)
    card = ProbeCard("test-identity", mgr, probe_timeout=5.0)

    calls_a: list[dict] = []
    calls_b: list[dict] = []

    def cb_a(r):
        calls_a.append(r)

    def cb_b(r):
        calls_b.append(r)

    card.subscribe(cb_a)
    card.subscribe(cb_b)
    assert card.subscriber_count == 2

    card.unsubscribe(cb_a)
    assert card.subscriber_count == 1

    await card.run_probe()

    assert len(calls_a) == 0
    assert len(calls_b) == 1


async def test_start_stop():
    """start() creates a live _probe_task; stop() cancels it cleanly."""
    session = MockSession(data=b"ok\n", alive=False)
    mgr = MockSessionManager(session)
    # Use a very long interval so the loop never fires a second probe during the test.
    card = ProbeCard("test-identity", mgr, probe_timeout=5.0, interval_seconds=3600)

    await card.start()

    assert card._probe_task is not None
    assert not card._probe_task.done()

    await card.stop()

    assert card._probe_task is None


async def test_probe_loop_cancel_while_inflight():
    """stop() cancels the probe loop cleanly even if a probe is in-flight (alive=True)."""
    # alive=True ensures run_probe will block inside the polling loop.
    session = MockSession(data=b"", alive=True)
    mgr = MockSessionManager(session)
    card = ProbeCard("test-identity", mgr, probe_timeout=60.0)

    # Manually create the loop task (skip start() initial probe to avoid blocking).
    card._probe_task = asyncio.create_task(card._probe_loop())

    # Give the loop a moment to enter sleep.
    await asyncio.sleep(0.05)

    assert not card._probe_task.done()

    # stop() must not hang — it cancels and awaits the task.
    await card.stop()

    assert card._probe_task is None


async def test_subscriber_count_initial():
    """A freshly created card has zero subscribers."""
    mgr = MockSessionManager()
    card = ProbeCard("test-identity", mgr)
    assert card.subscriber_count == 0


async def test_unsubscribe_nonexistent_is_safe():
    """Calling unsubscribe with an unknown callback does not raise."""
    mgr = MockSessionManager()
    card = ProbeCard("test-identity", mgr)
    card.unsubscribe(lambda r: None)  # should not raise


async def test_last_result_initially_none():
    """last_result is None before any successful probe."""
    mgr = MockSessionManager()
    card = ProbeCard("test-identity", mgr)
    assert card.last_result is None


async def test_card_type_and_hidden():
    """ProbeCard preserves its card_type; ServiceCard sets hidden=True."""
    mgr = MockSessionManager()
    card = ProbeCard("test-identity", mgr)
    assert card.card_type == "test-probe"
    assert card.hidden is True


async def test_run_probe_destroys_session_on_success():
    """destroy_session is called after a successful probe."""
    session = MockSession(data=b"data\n", alive=False)
    mgr = MockSessionManager(session)
    card = ProbeCard("test-identity", mgr, probe_timeout=5.0)

    await card.run_probe()

    assert mgr.destroyed == ["mock-session-01"]
