"""CardRegistry: unified lookup for all BaseCard instances (TerminalCard, ServiceCard, etc.)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from .base import BaseCard
from .terminal_card import TerminalCard

if TYPE_CHECKING:
    from claude_rts.event_bus import EventBus


class CardRegistry:
    """Server-level singleton that tracks every live card by its id.

    TerminalCards are registered/unregistered by the WebSocket handlers.
    ServiceCards continue to be managed by ServiceCardRegistry; this
    registry provides a single place to look up *any* card by id.

    When an ``EventBus`` is provided, the registry emits
    ``card:registered`` and ``card:unregistered`` events automatically.
    """

    def __init__(self, bus: EventBus | None = None):
        self._cards: dict[str, BaseCard] = {}
        self._bus = bus

    def register(self, card: BaseCard) -> None:
        """Add a card to the registry."""
        self._cards[card.id] = card
        logger.debug("CardRegistry: registered {} '{}'", card.card_type, card.id)
        if self._bus is not None:
            # Requires a running event loop — always true when called from aiohttp handlers.
            asyncio.ensure_future(self._bus.emit("card:registered", {"card_id": card.id, "card_type": card.card_type}))

    def unregister(self, card_id: str) -> BaseCard | None:
        """Remove and return a card, or None if not found."""
        card = self._cards.pop(card_id, None)
        if card:
            logger.debug("CardRegistry: unregistered {} '{}'", card.card_type, card_id)
            if self._bus is not None:
                asyncio.ensure_future(
                    self._bus.emit("card:unregistered", {"card_id": card.id, "card_type": card.card_type})
                )
        return card

    def get(self, card_id: str) -> BaseCard | None:
        """Look up a card by id."""
        return self._cards.get(card_id)

    def get_terminal(self, session_id: str) -> TerminalCard | None:
        """Look up a TerminalCard by session_id (convenience)."""
        card = self._cards.get(session_id)
        if isinstance(card, TerminalCard):
            return card
        return None

    def list_terminals(self) -> list[TerminalCard]:
        """Return all registered TerminalCards."""
        return [c for c in self._cards.values() if isinstance(c, TerminalCard)]

    def list_all(self) -> list[BaseCard]:
        """Return all registered cards."""
        return list(self._cards.values())

    async def stop_all(self) -> None:
        """Stop and remove all cards."""
        for card_id in list(self._cards.keys()):
            card = self._cards.pop(card_id, None)
            if card:
                try:
                    await card.stop()
                except Exception:
                    logger.exception("CardRegistry: error stopping card '{}'", card_id)
        logger.info("CardRegistry: all cards stopped")
