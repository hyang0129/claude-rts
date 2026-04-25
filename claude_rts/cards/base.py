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
        # Set by CardRegistry.register(); allows the card to call rekey() on
        # id mutation (TerminalCard.start() aligns _id to session_id).
        self._registry = None
        # Server-owned state (epic #236). Every field listed here must appear
        # in ``MUTABLE_FIELDS`` on the subclass that wants it mutable through
        # ``PUT /api/cards/{id}/state`` (see docs/state-model.md).
        self.starred: bool = False
        # Epic #236 child 4 (#240): position / size / z-order are server-owned.
        # Mutated only through ``CardRegistry.apply_state_patch`` (drag/resize/
        # focus all PUT to ``/api/cards/{id}/state`` on ``pointerup``); the
        # ``card_updated`` broadcast carries them to every connected client.
        # Defaults are 0; the spawn handler back-fills real values from the
        # query params, and the client renders optimistically during the
        # gesture and only commits on mouseup (DP-4).
        self.x: int = 0
        self.y: int = 0
        self.w: int = 0
        self.h: int = 0
        self.z_order: int = 0
        # Epic #254 child 2 (#257): recovery metadata for eager PTY creation.
        # Server-computed (not client-mutable, not in MUTABLE_FIELDS), not
        # persisted to the canvas JSON snapshot, and reset to ``None`` on every
        # server restart. Schema is locked to three keys:
        #   {"kind": str, "attempts": int, "last_error": str}
        # Child #3 consumes ``kind`` and ``attempts`` to render a retry button.
        self.error_state: dict | None = None

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

    @classmethod
    @abc.abstractmethod
    def from_descriptor(cls, data: dict, **kwargs) -> "BaseCard":
        """Reconstruct a card from a canvas-JSON snapshot entry.

        Epic #254 child 2 (#257): the hydration path calls ``from_descriptor``
        to build a card from disk, then invokes ``start()`` separately. Implementations
        must NOT start any background activity themselves — the caller owns
        lifecycle ordering so retry and error handling apply uniformly.

        Subclasses accept type-specific keyword arguments (e.g. ``session_manager``
        for ``TerminalCard``).
        """
