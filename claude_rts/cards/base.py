"""BaseCard: minimal abstract base class for all card types."""

import abc
import uuid


class BaseCard(abc.ABC):
    """Abstract base for all card types."""

    card_type: str = "base"
    hidden: bool = False  # ServiceCard overrides to True

    def __init__(self, card_id: str | None = None):
        self._id = card_id or uuid.uuid4().hex[:8]

    @property
    def id(self) -> str:
        return self._id

    @abc.abstractmethod
    async def start(self) -> None:
        """Start the card's background activity."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop the card's background activity and clean up."""
