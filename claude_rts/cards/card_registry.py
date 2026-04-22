"""CardRegistry: unified lookup for all BaseCard instances (TerminalCard, ServiceCard, etc.)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from .base import BaseCard
from .terminal_card import TerminalCard

if TYPE_CHECKING:
    from claude_rts.event_bus import EventBus
    from .canvas_claude_card import CanvasClaudeCard


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

    def apply_state_patch(self, card_id: str, fields: dict) -> dict:
        """Apply a partial state patch to the card with the given id.

        This is the **sole** attribute-mutation path for server-owned card
        fields — the generic ``PUT /api/cards/{id}/state`` handler and the
        legacy ``/rename`` + ``/recovery-script`` aliases all funnel through
        here (see issue #238 / state-model.md).

        Each key in ``fields`` must appear in the card's
        ``MUTABLE_FIELDS`` allowlist. Allowed value types per field are
        declared in ``MUTABLE_FIELD_TYPES`` on the card class (defaults to
        ``str`` for any field not listed there). Child 3 extends this to
        ``bool`` for ``starred``; Child 4 will extend to ``int`` / ``float``
        for position / size.

        Returns the dict of fields that were actually applied.

        Raises:
            LookupError: card_id is not in the registry.
            ValueError: a field is not in the card's allowlist or has the
                wrong type. The message identifies the offending field so
                callers can surface a structured 400.
        """
        card = self._cards.get(card_id)
        if card is None:
            raise LookupError(card_id)

        allowed = getattr(card, "MUTABLE_FIELDS", frozenset())
        field_types: dict = getattr(card, "MUTABLE_FIELD_TYPES", {})
        applied: dict = {}
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"field '{key}' is not mutable on card_type '{card.card_type}'")
            expected_type = field_types.get(key, str)
            # ``bool`` is a subclass of ``int`` in Python — enforce exact type
            # so a caller can't sneak a bool into an int field or vice-versa.
            if expected_type is bool:
                if not isinstance(value, bool):
                    raise ValueError(f"field '{key}' must be a boolean")
            elif expected_type is int:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError(f"field '{key}' must be an integer")
            elif not isinstance(value, expected_type):
                type_name = getattr(expected_type, "__name__", str(expected_type))
                raise ValueError(f"field '{key}' must be a {type_name}")
            setattr(card, key, value)
            applied[key] = value

        # Epic #236 child 4 (#240): track which geometry fields have been
        # explicitly mutated so ``to_descriptor`` can decide whether to emit
        # them. A card that has never received an explicit ``x``/``y``/``w``/
        # ``h``/``z_order`` (constructor or PUT) keeps its default-0 values
        # internal and the frontend's viewport-center fallback applies.
        explicit = getattr(card, "_explicit_geometry", None)
        if explicit is not None:
            for key in applied:
                if key in {"x", "y", "w", "h", "z_order"}:
                    explicit.add(key)

        if applied:
            logger.debug("CardRegistry: patched card '{}' fields={}", card_id, list(applied.keys()))
        return applied

    def get_terminal(self, session_id: str) -> TerminalCard | None:
        """Look up a TerminalCard by session_id (convenience)."""
        card = self._cards.get(session_id)
        if isinstance(card, TerminalCard):
            return card
        return None

    def list_terminals(self) -> list[TerminalCard]:
        """Return all registered TerminalCards."""
        return [c for c in self._cards.values() if isinstance(c, TerminalCard)]

    def by_type(self, card_type: str) -> list[BaseCard]:
        """Return all cards whose ``card_type`` matches ``card_type``.

        This is the type-string equivalent of ``list_terminals``/
        ``list_canvas_claude`` and is the preferred way for generic code to
        fetch a homogeneous subset of the registry without importing the
        concrete subclass.
        """
        return [c for c in self._cards.values() if c.card_type == card_type]

    def get_canvas_claude(self, card_id: str) -> "CanvasClaudeCard | None":
        """Look up a CanvasClaudeCard by id."""
        from .canvas_claude_card import CanvasClaudeCard

        card = self._cards.get(card_id)
        if isinstance(card, CanvasClaudeCard):
            return card
        return None

    def list_canvas_claude(self) -> list:
        """Return all registered CanvasClaudeCards."""
        from .canvas_claude_card import CanvasClaudeCard

        return [c for c in self._cards.values() if isinstance(c, CanvasClaudeCard)]

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
