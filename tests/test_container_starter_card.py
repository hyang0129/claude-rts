"""Tests for ContainerStarterCard: readiness probe, event emission, self-close."""

import asyncio


from claude_rts.cards.container_starter_card import ContainerStarterCard
from claude_rts.cards.card_registry import CardRegistry
from claude_rts.event_bus import EventBus


def _make_app_dict():
    """Create a minimal app dict for testing."""
    bus = EventBus()
    reg = CardRegistry(bus=bus)
    app = {
        "event_bus": bus,
        "card_registry": reg,
    }
    return app, bus, reg


# ── Basic properties ────────────────────────────────────────────────────────


def test_card_type():
    card = ContainerStarterCard(container_name="test")
    assert card.card_type == "container_starter"
    assert card.hidden is True


def test_container_name():
    card = ContainerStarterCard(container_name="my-container")
    assert card.container_name == "my-container"


# ── Success path (mock containers) ──────────────────────────────────────────


async def test_start_success_emits_ready():
    """ContainerStarterCard starts container and emits container:ready:{name}."""
    app, bus, reg = _make_app_dict()
    app["_test_vm_containers"] = [{"name": "hub1", "state": "offline"}]

    events = []

    async def capture(event_type, payload):
        events.append((event_type, payload))

    bus.subscribe("container:ready:hub1", capture)

    card = ContainerStarterCard(container_name="hub1", app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    # Should have emitted container:ready:hub1
    ready_events = [e for e in events if e[0] == "container:ready:hub1"]
    assert len(ready_events) == 1
    assert ready_events[0][1]["container_name"] == "hub1"

    # Container state should be "online"
    assert app["_test_vm_containers"][0]["state"] == "online"


async def test_self_close_after_success():
    """Card unregisters from CardRegistry after successful start."""
    app, bus, reg = _make_app_dict()
    app["_test_vm_containers"] = [{"name": "hub1", "state": "offline"}]

    card = ContainerStarterCard(container_name="hub1", app=app)
    card.bus = bus
    reg.register(card)
    card_id = card.id

    assert reg.get(card_id) is not None

    await card.start()
    await asyncio.sleep(0.5)

    # Card should have self-unregistered
    assert reg.get(card_id) is None


# ── Failure path ────────────────────────────────────────────────────────────


async def test_start_failure_emits_failed():
    """Starting a nonexistent container emits container:failed:{name}."""
    app, bus, reg = _make_app_dict()
    app["_test_vm_containers"] = []  # No containers

    events = []

    async def capture(event_type, payload):
        events.append((event_type, payload))

    bus.subscribe("container:failed:nope", capture)

    card = ContainerStarterCard(container_name="nope", app=app)
    card.bus = bus
    reg.register(card)

    await card.start()
    await asyncio.sleep(0.5)

    failed_events = [e for e in events if e[0] == "container:failed:nope"]
    assert len(failed_events) == 1
    assert "error" in failed_events[0][1]


async def test_self_close_after_failure():
    """Card unregisters from CardRegistry even after failure."""
    app, bus, reg = _make_app_dict()
    app["_test_vm_containers"] = []

    card = ContainerStarterCard(container_name="nope", app=app)
    card.bus = bus
    reg.register(card)
    card_id = card.id

    await card.start()
    await asyncio.sleep(0.5)

    assert reg.get(card_id) is None


# ── Stop/cancel ─────────────────────────────────────────────────────────────


async def test_stop_cancels_task():
    """stop() cancels the running task."""
    app, bus, reg = _make_app_dict()
    app["_test_vm_containers"] = [{"name": "hub1", "state": "offline"}]

    card = ContainerStarterCard(container_name="hub1", app=app)
    card.bus = bus

    await card.start()
    assert card._task is not None

    await card.stop()
    assert card._task is None


# ── Custom timeout ──────────────────────────────────────────────────────────


def test_custom_timeout():
    card = ContainerStarterCard(container_name="test", timeout=30.0)
    assert card._timeout == 30.0


def test_default_timeout():
    card = ContainerStarterCard(container_name="test")
    assert card._timeout == ContainerStarterCard.DEFAULT_TIMEOUT
