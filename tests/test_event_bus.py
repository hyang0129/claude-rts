"""Tests for EventBus: subscribe, emit, unsubscribe, wildcard, error handling, clear, integration."""

import asyncio

from claude_rts.event_bus import EventBus
from tests.conftest import ProbeCard, MockSession, MockSessionManager


# ── Core EventBus tests ────────────────────────────────────────────────────


async def test_subscribe_and_emit():
    """subscribe + emit delivers (event_type, payload) to the callback."""
    bus = EventBus()
    received: list[tuple] = []
    bus.subscribe("probe:test", lambda et, p: received.append((et, p)))

    await bus.emit("probe:test", {"value": 42})

    assert len(received) == 1
    assert received[0] == ("probe:test", {"value": 42})


async def test_unsubscribe_stops_delivery():
    """After unsubscribe, the callback no longer receives events."""
    bus = EventBus()
    received: list[dict] = []

    def cb(et, p):
        received.append(p)

    bus.subscribe("x", cb)
    await bus.emit("x", {"a": 1})
    assert len(received) == 1

    bus.unsubscribe("x", cb)
    await bus.emit("x", {"a": 2})
    assert len(received) == 1  # no new delivery


async def test_wildcard_receives_all():
    """A '*' subscriber receives events of every type."""
    bus = EventBus()
    received: list[str] = []
    bus.subscribe("*", lambda et, p: received.append(et))

    await bus.emit("probe:usage", {})
    await bus.emit("card:registered", {})
    await bus.emit("terminal:started", {})

    assert received == ["probe:usage", "card:registered", "terminal:started"]


async def test_async_callback_awaited():
    """Async callbacks are scheduled and eventually invoked."""
    bus = EventBus()
    received: list[dict] = []

    async def async_cb(et, p):
        received.append(p)

    bus.subscribe("test", async_cb)
    await bus.emit("test", {"async": True})

    # Give event loop time to run fire-and-forget task
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0] == {"async": True}


async def test_subscriber_exception_does_not_block():
    """An exception in one subscriber does not prevent delivery to others."""
    bus = EventBus()
    received: list[str] = []

    def bad_cb(et, p):
        raise RuntimeError("boom")

    def good_cb(et, p):
        received.append("ok")

    bus.subscribe("ev", bad_cb)
    bus.subscribe("ev", good_cb)

    await bus.emit("ev", {})

    assert received == ["ok"]


async def test_async_subscriber_exception_is_logged():
    """An async callback that raises still allows other subscribers to run and gets logged."""
    bus = EventBus()
    received: list[str] = []

    async def bad_async_cb(et, p):
        raise RuntimeError("async boom")

    def good_cb(et, p):
        received.append("ok")

    bus.subscribe("ev", bad_async_cb)
    bus.subscribe("ev", good_cb)

    await bus.emit("ev", {})

    # Give the async task time to complete and log the error
    await asyncio.sleep(0.05)

    # The sync subscriber still received the event
    assert received == ["ok"]
    # The async task should have completed (error was logged, not propagated)
    assert len(bus._pending_tasks) == 0


async def test_clear_removes_all_subscriptions():
    """clear() wipes all subscriptions; subsequent emit delivers nothing."""
    bus = EventBus()
    received: list[str] = []
    bus.subscribe("a", lambda et, p: received.append("a"))
    bus.subscribe("b", lambda et, p: received.append("b"))
    bus.subscribe("*", lambda et, p: received.append("*"))

    bus.clear()

    await bus.emit("a", {})
    await bus.emit("b", {})
    assert received == []


async def test_unsubscribe_nonexistent_is_safe():
    """Calling unsubscribe with an unknown callback or event type does not raise."""
    bus = EventBus()
    bus.unsubscribe("no-such-event", lambda et, p: None)  # should not raise


async def test_multiple_subscribers_same_event():
    """Multiple subscribers on the same event all receive the payload."""
    bus = EventBus()
    results: list[int] = []

    bus.subscribe("ev", lambda et, p: results.append(1))
    bus.subscribe("ev", lambda et, p: results.append(2))
    bus.subscribe("ev", lambda et, p: results.append(3))

    await bus.emit("ev", {})

    assert results == [1, 2, 3]


async def test_wildcard_and_specific_both_fire():
    """Both specific and wildcard subscribers receive the same event."""
    bus = EventBus()
    tags: list[str] = []

    bus.subscribe("ev", lambda et, p: tags.append("specific"))
    bus.subscribe("*", lambda et, p: tags.append("wildcard"))

    await bus.emit("ev", {})

    assert "specific" in tags
    assert "wildcard" in tags
    assert len(tags) == 2


# ── Integration: ServiceCard emits on bus after probe ──────────────────────


async def test_service_card_emits_on_bus():
    """When a ServiceCard has a bus, _notify_subscribers also emits probe:{card_type}."""
    bus = EventBus()
    bus_received: list[tuple] = []
    bus.subscribe("probe:test-probe", lambda et, p: bus_received.append((et, p)))

    session = MockSession(data=b"hello\n", alive=False)
    mgr = MockSessionManager(session)
    card = ProbeCard("test-id", mgr, probe_timeout=5.0)
    card.bus = bus

    result = await card.run_probe()

    assert result is not None
    assert len(bus_received) == 1
    assert bus_received[0][0] == "probe:test-probe"
    assert bus_received[0][1] == result


async def test_service_card_no_bus_still_works():
    """ServiceCard without a bus continues to work via legacy _subscribers."""
    session = MockSession(data=b"data\n", alive=False)
    mgr = MockSessionManager(session)
    card = ProbeCard("no-bus", mgr, probe_timeout=5.0)

    legacy: list[dict] = []
    card.subscribe(lambda r: legacy.append(r))

    result = await card.run_probe()

    assert result is not None
    assert len(legacy) == 1


# ── Integration: CardRegistry emits card:registered / card:unregistered ────


async def test_card_registry_emits_registered():
    """CardRegistry emits card:registered when a card is added."""
    bus = EventBus()
    events: list[dict] = []
    bus.subscribe("card:registered", lambda et, p: events.append(p))

    from claude_rts.cards.card_registry import CardRegistry
    from claude_rts.cards.terminal_card import TerminalCard

    reg = CardRegistry(bus=bus)
    mgr = MockSessionManager()
    card = TerminalCard(session_manager=mgr, cmd="echo hi", card_id="test-123")

    reg.register(card)

    # Give event loop time for asyncio.ensure_future
    await asyncio.sleep(0.05)

    assert len(events) == 1
    assert events[0]["card_id"] == "test-123"
    assert events[0]["card_type"] == "terminal"


async def test_card_registry_emits_unregistered():
    """CardRegistry emits card:unregistered when a card is removed."""
    bus = EventBus()
    events: list[dict] = []
    bus.subscribe("card:unregistered", lambda et, p: events.append(p))

    from claude_rts.cards.card_registry import CardRegistry
    from claude_rts.cards.terminal_card import TerminalCard

    reg = CardRegistry(bus=bus)
    mgr = MockSessionManager()
    card = TerminalCard(session_manager=mgr, cmd="echo hi", card_id="test-456")
    reg.register(card)

    reg.unregister("test-456")

    await asyncio.sleep(0.05)

    assert len(events) == 1
    assert events[0]["card_id"] == "test-456"
    assert events[0]["card_type"] == "terminal"
