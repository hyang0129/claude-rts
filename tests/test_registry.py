"""Tests for ServiceCardRegistry: subscribe-or-reuse semantics, auto-stop, stop_all."""

import pytest

from claude_rts.cards.registry import ServiceCardRegistry
from claude_rts.cards.service_card import ServiceCard
from tests.conftest import ProbeCard, MockSessionManager


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_registry() -> tuple[ServiceCardRegistry, MockSessionManager]:
    mgr = MockSessionManager()
    registry = ServiceCardRegistry(mgr)
    registry.register_type("test-probe", ProbeCard)
    return registry, mgr


def noop_callback(result: dict) -> None:
    pass


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_subscribe_creates_card():
    """Subscribing to a new key creates a card and makes it retrievable via get()."""
    registry, _ = make_registry()

    card = await registry.subscribe("test-probe", "identity-1", noop_callback, interval_seconds=3600)

    assert card is not None
    assert registry.get("test-probe", "identity-1") is card

    await registry.stop_all()


async def test_subscribe_reuse():
    """Subscribing two callbacks to the same (type, identity) reuses one card."""
    registry, _ = make_registry()

    calls_a: list[dict] = []
    calls_b: list[dict] = []

    card_a = await registry.subscribe("test-probe", "identity-1", lambda r: calls_a.append(r), interval_seconds=3600)
    card_b = await registry.subscribe("test-probe", "identity-1", lambda r: calls_b.append(r), interval_seconds=3600)

    assert card_a is card_b
    assert card_a.subscriber_count == 2
    # The second subscribe should immediately deliver the cached last_result to calls_b.
    assert len(calls_b) >= 1

    await registry.stop_all()


async def test_subscribe_different_identities():
    """Subscribing to two different identities creates two distinct card instances."""
    registry, _ = make_registry()

    card_a = await registry.subscribe("test-probe", "identity-a", noop_callback, interval_seconds=3600)
    card_b = await registry.subscribe("test-probe", "identity-b", noop_callback, interval_seconds=3600)

    assert card_a is not card_b
    assert registry.get("test-probe", "identity-a") is card_a
    assert registry.get("test-probe", "identity-b") is card_b

    await registry.stop_all()


async def test_unsubscribe_auto_stop():
    """Unsubscribing the last callback stops the card and removes it from the registry."""
    registry, _ = make_registry()

    cb = noop_callback
    await registry.subscribe("test-probe", "identity-1", cb, interval_seconds=3600)
    await registry.unsubscribe("test-probe", "identity-1", cb)

    assert registry.get("test-probe", "identity-1") is None


async def test_unsubscribe_keeps_card_with_remaining_subscribers():
    """Unsubscribing one of two callbacks keeps the card alive."""
    registry, _ = make_registry()

    calls_a: list[dict] = []
    calls_b: list[dict] = []

    def cb_a(r):
        calls_a.append(r)

    def cb_b(r):
        calls_b.append(r)

    await registry.subscribe("test-probe", "identity-1", cb_a, interval_seconds=3600)
    await registry.subscribe("test-probe", "identity-1", cb_b, interval_seconds=3600)

    await registry.unsubscribe("test-probe", "identity-1", cb_a)

    card = registry.get("test-probe", "identity-1")
    assert card is not None
    assert card.subscriber_count == 1

    await registry.stop_all()


async def test_stop_all():
    """stop_all stops every card and leaves the registry empty."""
    registry, _ = make_registry()

    cb1 = noop_callback

    def cb2(r):
        pass

    await registry.subscribe("test-probe", "identity-1", cb1, interval_seconds=3600)
    await registry.subscribe("test-probe", "identity-2", cb2, interval_seconds=3600)

    await registry.stop_all()

    assert registry.get("test-probe", "identity-1") is None
    assert registry.get("test-probe", "identity-2") is None


async def test_unknown_type_raises():
    """Subscribing to an unregistered card_type raises KeyError."""
    registry, _ = make_registry()

    with pytest.raises(KeyError):
        await registry.subscribe("no-such-type", "identity-1", noop_callback)


async def test_get_returns_none_for_missing_key():
    """get() returns None when the (type, identity) pair has never been registered."""
    registry, _ = make_registry()
    assert registry.get("test-probe", "never-registered") is None


async def test_register_type_multiple():
    """Two different card_types can coexist in the registry without interference."""

    class AnotherCard(ServiceCard):
        card_type = "another"

        def probe_command(self) -> str:
            return "echo other"

        def parse_output(self, output: str) -> dict:
            return {"other": True}

    mgr = MockSessionManager()
    registry = ServiceCardRegistry(mgr)
    registry.register_type("test-probe", ProbeCard)
    registry.register_type("another", AnotherCard)

    card_a = await registry.subscribe("test-probe", "id-1", noop_callback, interval_seconds=3600)
    card_b = await registry.subscribe("another", "id-1", noop_callback, interval_seconds=3600)

    assert card_a is not card_b
    assert card_a.card_type == "test-probe"
    assert card_b.card_type == "another"

    await registry.stop_all()


async def test_unsubscribe_nonexistent_identity_is_safe():
    """Calling unsubscribe for an identity that doesn't exist does not raise."""
    registry, _ = make_registry()
    # Should complete without exception.
    await registry.unsubscribe("test-probe", "ghost-identity", noop_callback)


async def test_stop_all_empty_registry():
    """stop_all on an empty registry is a no-op and does not raise."""
    registry, _ = make_registry()
    await registry.stop_all()  # should not raise


async def test_subscribe_card_is_started():
    """A newly subscribed card's _probe_task is set (start was called)."""
    registry, _ = make_registry()

    card = await registry.subscribe("test-probe", "identity-1", noop_callback, interval_seconds=3600)

    assert card._probe_task is not None

    await registry.stop_all()
