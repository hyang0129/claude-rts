"""TerminalCard: first-class card wrapping an interactive PTY session."""

from loguru import logger
from .base import BaseCard


class TerminalCard(BaseCard):
    """Visible card that wraps a persistent PTY session.

    Lifecycle:
      - start() allocates a PTY via SessionManager.create_session()
      - stop()  destroys the PTY via SessionManager.destroy_session()

    The card's ``id`` doubles as the session_id, so the CardRegistry
    and SessionManager share a single key space.
    """

    card_type: str = "terminal"
    hidden: bool = False

    # Server-owned fields that ``PUT /api/cards/{id}/state`` may mutate.
    # See ``BaseCard.MUTABLE_FIELDS`` for the contract.
    MUTABLE_FIELDS: frozenset[str] = frozenset(
        {
            "display_name",
            "recovery_script",
            "starred",
            # Epic #236 child 4 (#240): position / size / z-order are
            # server-owned and committed on drag/resize/focus mouseup.
            "x",
            "y",
            "w",
            "h",
            "z_order",
        }
    )
    # Per-field expected type for ``CardRegistry.apply_state_patch`` validation.
    # Fields not listed here default to ``str``. Child 3 adds ``starred: bool``;
    # child 4 adds ``x``/``y``/``w``/``h``/``z_order`` as ``int``.
    MUTABLE_FIELD_TYPES: dict = {
        "starred": bool,
        "x": int,
        "y": int,
        "w": int,
        "h": int,
        "z_order": int,
    }

    def __init__(
        self,
        session_manager,
        cmd: str,
        hub: str | None = None,
        container: str | None = None,
        card_id: str | None = None,
        layout: dict | None = None,
        display_name: str | None = None,
        recovery_script: str | None = None,
        starred: bool = False,
        card_uid: str | None = None,
    ):
        # card_id is set *after* start() when we know the session_id,
        # unless the caller supplies one (reconnect path).
        super().__init__(card_id=card_id)
        self._session_manager = session_manager
        self.cmd = cmd
        self.hub = hub
        self.container = container
        self._session = None  # set by start()
        # Epic #236 child 4 (#240): the legacy ``layout`` dict is read **once**
        # at construction time as an initialisation source for the new
        # first-class server-owned position/size attributes (declared on
        # ``BaseCard``). After this point ``self.layout`` is retained for
        # backward compatibility with any direct readers but the authoritative
        # values live in ``self.x/y/w/h/z_order``. Drag/resize/focus on the
        # client commit through ``PUT /api/cards/{id}/state``.
        self.layout = layout or {}
        # Track whether the card has explicit position / size set — either at
        # construction (via the ``layout={...}`` spawn hint) or later via
        # ``apply_state_patch``. Used by ``to_descriptor`` to decide whether
        # to emit these fields, so the frontend's viewport-center fallback in
        # ``handleControlCardCreated`` keeps working for ad-hoc server spawns.
        self._explicit_geometry: set[str] = set()
        if self.layout:
            for _field in ("x", "y", "w", "h", "z_order"):
                _val = self.layout.get(_field)
                if isinstance(_val, int) and not isinstance(_val, bool):
                    setattr(self, _field, _val)
                    self._explicit_geometry.add(_field)
        self.display_name = display_name or ""
        self.recovery_script = recovery_script or ""
        # Epic #236 child 3: ``starred`` is server-owned (see docs/state-model.md).
        # Mutated only through ``CardRegistry.apply_state_patch`` and broadcast
        # via ``card_updated``; never assigned directly by the client.
        self.starred = bool(starred)
        # Stable identity UUID across reconnects/reloads. Client generates it
        # on first spawn (``crypto.randomUUID``) and ships it via the WS spawn
        # query (?card_uid=...) — the client is a courier, not the author. The
        # server stores it and emits it in ``to_descriptor`` as ``card_uid``;
        # it is immutable for the lifetime of the card (no MUTABLE_FIELDS entry).
        self.card_uid = (card_uid or "").strip()

    # ── Descriptor serialization ───────────────────────────────────────

    def to_descriptor(self) -> dict:
        """Return the JSON-serializable descriptor the frontend expects.

        Shape matches the TerminalCard.serialize() output in index.html:
            { type, hub, container, exec, session_id }
        """
        desc: dict = {
            "type": self.card_type,
            "session_id": self.id,
            # Epic #236 child 5 (#241): every server-authored descriptor
            # includes ``card_id`` — it is the schema discriminator for the
            # canvas-snapshot file (no ``card_id`` on a card entry == old
            # client-authored format). For terminals, ``card_id == session_id``.
            "card_id": self.id,
            # Epic #236 child 3: ``starred`` is always included — both True and
            # False — so the client boot path can use it as the authoritative
            # value without needing to fall back to a legacy default.
            "starred": bool(self.starred),
        }
        if self.card_uid:
            desc["card_uid"] = self.card_uid
        if self.hub:
            desc["hub"] = self.hub
        if self.container:
            desc["container"] = self.container
        if self.cmd:
            desc["exec"] = self.cmd
        if self.display_name:
            desc["display_name"] = self.display_name
        if self.recovery_script:
            desc["recovery_script"] = self.recovery_script
        # Epic #236 child 4 (#240): position / size / z-order are server-owned
        # and emitted only when explicitly set (via the ``layout={...}`` spawn
        # hint or a ``PUT /api/cards/{id}/state`` patch). Default values are
        # omitted so the frontend's existing ``desc.x != null ? ... :
        # viewport-center`` fallback at handleControlCardCreated keeps placing
        # ad-hoc spawns near the user's view instead of pinning them to (0,0).
        for _field in ("x", "y", "w", "h", "z_order"):
            if _field in self._explicit_geometry:
                desc[_field] = int(getattr(self, _field))
        return desc

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Allocate a PTY session via SessionManager."""
        session = self._session_manager.create_session(
            self.cmd,
            hub=self.hub,
            container=self.container,
        )
        self._session = session
        # Align card id with session_id so lookups are unified.
        self._id = session.session_id
        logger.info(
            "TerminalCard {}: started (cmd={!r}, hub={}, container={})",
            self.id,
            self.cmd,
            self.hub,
            self.container,
        )

    async def stop(self) -> None:
        """Destroy the underlying PTY session.

        kill_tmux is False by default; tmux sessions inside containers
        persist for recovery on the next server start.  The server's
        stop_all path already cleans up gracefully.
        """
        if self._session is not None:
            sid = self._session.session_id
            self._session_manager.destroy_session(sid, kill_tmux=False)
            logger.info("TerminalCard {}: stopped", sid)
            self._session = None

    # ── Convenience accessors ─────────────────────────────────────────

    @property
    def session(self):
        """Return the underlying Session object (or None if not started)."""
        return self._session

    @property
    def session_id(self) -> str:
        """Alias for id — the session_id doubles as the card id."""
        return self.id

    @property
    def alive(self) -> bool:
        """True if the underlying PTY session is still running."""
        return self._session is not None and self._session.alive
