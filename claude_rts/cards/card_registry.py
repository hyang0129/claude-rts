"""CardRegistry: unified lookup for all BaseCard instances (TerminalCard, ServiceCard, etc.)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable

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

    Epic #236 child 5 (#241): the registry also tracks each card's canvas
    membership (``card_id → canvas_name``) and exposes a
    ``persist_callback`` hook called from ``apply_state_patch`` so the
    server can rewrite the canvas JSON snapshot whenever a server-owned
    field is mutated. The callback is the only path that touches disk —
    callers never invoke ``write_state_snapshot`` directly.
    """

    def __init__(
        self,
        bus: EventBus | None = None,
        persist_callback: Callable[[str], None] | None = None,
    ):
        self._cards: dict[str, BaseCard] = {}
        # Canvas membership: card_id -> canvas_name. ``None`` is allowed (the
        # card is registered but not yet associated with a canvas, e.g. a
        # service card created before the user opens any canvas). The
        # persistence hook silently no-ops for cards with no canvas.
        self._canvas_map: dict[str, str | None] = {}
        # Stable insertion order for ``cards_on_canvas``. Updated by
        # ``register`` and ``rekey`` so that rekeys do not move cards to the
        # end of the iteration order (Python dict moves an entry to the end
        # when you pop-and-reassign, which would break canvas snapshot order).
        self._order: list[str] = []
        self._bus = bus
        self._persist_callback = persist_callback

    def register(self, card: BaseCard, canvas_name: str | None = None) -> None:
        """Add a card to the registry.

        ``canvas_name`` records which canvas this card belongs to, so the
        persistence hook in ``apply_state_patch`` can rewrite the right
        on-disk snapshot. ``None`` means the card has no canvas yet — its
        mutations will not trigger a write-through. Pass the active canvas
        name from the spawn handler (it lives on the request as a query
        parameter or in the canvas_claude card config).
        """
        self._cards[card.id] = card
        self._canvas_map[card.id] = canvas_name
        if card.id not in self._order:
            self._order.append(card.id)
        # Back-reference so the card can call rekey() after id mutation (e.g.
        # TerminalCard.start() changes self._id to the real session_id).
        card._registry = self
        logger.debug(
            "CardRegistry: registered {} '{}' on canvas '{}'",
            card.card_type,
            card.id,
            canvas_name,
        )
        if self._bus is not None:
            # Requires a running event loop — always true when called from aiohttp handlers.
            asyncio.ensure_future(self._bus.emit("card:registered", {"card_id": card.id, "card_type": card.card_type}))

    def rekey(self, old_id: str, new_id: str) -> None:
        """Rename a card's registry key from ``old_id`` to ``new_id``.

        Called from TerminalCard.start() after ``self._id`` is updated to the
        real session_id so that subsequent get_terminal(session_id) lookups
        find the card.  Both ``_cards`` and ``_canvas_map`` are updated
        atomically; if ``old_id`` is not present this is a no-op.
        """
        if old_id == new_id:
            return
        card = self._cards.pop(old_id, None)
        if card is None:
            return
        canvas_name = self._canvas_map.pop(old_id, None)
        self._cards[new_id] = card
        self._canvas_map[new_id] = canvas_name
        # Update _order in-place so the card keeps its original position.
        try:
            idx = self._order.index(old_id)
            self._order[idx] = new_id
        except ValueError:
            self._order.append(new_id)
        logger.debug(
            "CardRegistry: rekeyed {} '{}' → '{}'",
            card.card_type,
            old_id,
            new_id,
        )
        # Trigger a canvas persist so the on-disk JSON reflects the new
        # session_id. Without this, the shim in cards_list_handler would
        # see the old placeholder id in the snapshot and return it as a
        # duplicate descriptor alongside the registry's real session_id.
        self._persist_canvas(canvas_name)

    def unregister(self, card_id: str) -> BaseCard | None:
        """Remove and return a card, or None if not found."""
        card = self._cards.pop(card_id, None)
        canvas_name = self._canvas_map.pop(card_id, None)
        try:
            self._order.remove(card_id)
        except ValueError:
            pass
        if card:
            logger.debug("CardRegistry: unregistered {} '{}'", card.card_type, card_id)
            if self._bus is not None:
                asyncio.ensure_future(
                    self._bus.emit("card:unregistered", {"card_id": card.id, "card_type": card.card_type})
                )
            # Persist after removal so the snapshot reflects the new
            # registry state. Empty canvases still get a write — the
            # frontend will read an empty cards array and start fresh.
            self._persist_canvas(canvas_name)
        return card

    # ── Canvas membership / persistence ─────────────────────────────

    def get_canvas_name(self, card_id: str) -> str | None:
        """Return the canvas a card is registered under, or None."""
        return self._canvas_map.get(card_id)

    def cards_on_canvas(self, canvas_name: str) -> list[BaseCard]:
        """Return every registered card belonging to ``canvas_name``.

        Cards are returned in original registration order (stable across
        rekeys, which would otherwise move entries to the end of the dict).
        """
        return [
            self._cards[cid] for cid in self._order if cid in self._cards and self._canvas_map.get(cid) == canvas_name
        ]

    def set_persist_callback(self, callback: Callable[[str], None] | None) -> None:
        """Wire (or clear) the write-through hook.

        The callback receives one argument: the canvas name to persist. It
        is invoked synchronously from ``apply_state_patch`` and from
        ``unregister``. Callers are responsible for catching their own
        exceptions; the registry only logs them.
        """
        self._persist_callback = callback

    def _persist_canvas(self, canvas_name: str | None) -> None:
        """Invoke the persist callback if one is wired and a canvas is set."""
        if canvas_name is None or self._persist_callback is None:
            return
        try:
            self._persist_callback(canvas_name)
        except Exception:
            logger.exception("CardRegistry: persist callback failed for canvas '{}'", canvas_name)

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
            # Epic #236 child 5 (#241): write-through to canvas JSON. The
            # snapshot rewrite is synchronous (per the TIMEBOX note in the
            # decomposition — debounce is explicitly deferred). Errors are
            # swallowed by ``_persist_canvas`` so a disk failure does not
            # roll back the in-memory mutation; the broadcast still goes out.
            self._persist_canvas(self._canvas_map.get(card_id))
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
        self._canvas_map.clear()
        self._order.clear()
        logger.info("CardRegistry: all cards stopped")
