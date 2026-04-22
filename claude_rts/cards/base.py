"""BaseCard: minimal abstract base class for all card types."""

from __future__ import annotations

import abc
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_rts.event_bus import EventBus


class BaseCard(abc.ABC):
    """Abstract base for all card types."""

    card_type: str = "base"
    hidden: bool = False  # ServiceCard overrides to True

    # Allowlist of attribute names that may be mutated through
    # ``CardRegistry.apply_state_patch`` (the single server-owned mutation path
    # defined by epic #236 / issue #238). Subclasses extend this set with any
    # additional server-owned fields. Fields outside this set are rejected with
    # HTTP 400 by ``PUT /api/cards/{id}/state``.
    #
    # To add a new server-owned field: add its name here (or on a subclass),
    # ensure it is a plain ``str`` attribute on the card instance, and add a
    # one-line dispatch entry in ``handleControlCardUpdated`` in
    # ``static/index.html``.
    MUTABLE_FIELDS: frozenset[str] = frozenset()

    def __init__(self, card_id: str | None = None, bus: EventBus | None = None):
        self._id = card_id or uuid.uuid4().hex[:8]
        self._bus = bus
        # Server-owned state (epic #236). Every field listed here must appear
        # in ``MUTABLE_FIELDS`` on the subclass that wants it mutable through
        # ``PUT /api/cards/{id}/state`` (see docs/state-model.md).
        self.starred: bool = False

    @property
    def id(self) -> str:
        return self._id

    @property
    def bus(self) -> EventBus | None:
        """The EventBus instance, if injected at construction."""
        return self._bus

    @bus.setter
    def bus(self, value: EventBus) -> None:
        self._bus = value

    @abc.abstractmethod
    async def start(self) -> None:
        """Start the card's background activity."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop the card's background activity and clean up."""
