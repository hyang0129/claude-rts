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

    def __init__(self, card_id: str | None = None, bus: EventBus | None = None):
        self._id = card_id or uuid.uuid4().hex[:8]
        self._bus = bus

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
