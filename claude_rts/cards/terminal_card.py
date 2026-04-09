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

    def __init__(
        self,
        session_manager,
        cmd: str,
        hub: str | None = None,
        container: str | None = None,
        card_id: str | None = None,
        layout: dict | None = None,
    ):
        # card_id is set *after* start() when we know the session_id,
        # unless the caller supplies one (reconnect path).
        super().__init__(card_id=card_id)
        self._session_manager = session_manager
        self.cmd = cmd
        self.hub = hub
        self.container = container
        self._session = None  # set by start()
        self.layout = layout or {}  # optional {x, y, w, h} hints for frontend

    # ── Descriptor serialization ───────────────────────────────────────

    def to_descriptor(self) -> dict:
        """Return the JSON-serializable descriptor the frontend expects.

        Shape matches the TerminalCard.serialize() output in index.html:
            { type, hub, container, exec, session_id }
        """
        desc: dict = {
            "type": self.card_type,
            "session_id": self.id,
        }
        if self.hub:
            desc["hub"] = self.hub
        if self.container:
            desc["container"] = self.container
        if self.cmd:
            desc["exec"] = self.cmd
        if self.layout:
            desc.update(self.layout)
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
