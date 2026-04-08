"""ServiceCard: abstract base for server-side headed PTY probe runners."""

import abc
import asyncio
import inspect
import time
from typing import Callable
from loguru import logger
from .base import BaseCard


class ServiceCard(BaseCard):
    """Hidden card that runs headed PTY probes on the server.

    Subclasses implement probe_command() and parse_output().
    Results are delivered to registered subscriber callbacks.
    """

    card_type: str = "service"
    hidden: bool = True  # Never visible in canvas UI or serialized

    # Per-credential probe cooldown (seconds). Probes for the same identity
    # within this window return the cached result instead of spawning a new PTY.
    PROBE_COOLDOWN_SECONDS: float = 300.0

    # Class-level dict: credential identity → monotonic timestamp of last probe.
    # Shared across all instances so two cards with the same identity share a cooldown.
    _probe_cooldowns: dict[str, float] = {}

    def __init__(
        self,
        identity: str,
        session_manager,  # SessionManager — typed loosely to avoid circular import
        container: str | None = None,
        probe_timeout: float = 60.0,
        interval_seconds: int = 900,
        card_id: str | None = None,
    ):
        super().__init__(card_id=card_id)
        self.identity = identity
        self._session_manager = session_manager
        self._container = container
        self._probe_timeout = probe_timeout
        self._subscribers: list[Callable] = []
        self._probe_task: asyncio.Task | None = None
        self._pending_tasks: set[asyncio.Task] = set()
        self._last_result: dict | None = None
        self._interval_seconds: int = interval_seconds

    @abc.abstractmethod
    def probe_command(self) -> str:
        """Return the shell command to run for this probe."""

    @abc.abstractmethod
    def parse_output(self, output: str) -> dict:
        """Parse raw PTY output into a structured result dict."""

    def subscribe(self, callback: Callable) -> None:
        """Register a callback(result: dict) called after each successful probe."""
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable) -> None:
        """Remove a previously registered callback."""
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def last_result(self) -> dict | None:
        return self._last_result

    async def run_probe(self) -> dict | None:
        """Spawn a headed PTY, run probe_command(), collect output, parse, notify subscribers.

        Returns parsed result dict, or None on failure/timeout.
        Skips the probe and returns cached result if the same credential identity
        was probed within PROBE_COOLDOWN_SECONDS.
        """
        # Per-credential cooldown check
        last_probe_time = ServiceCard._probe_cooldowns.get(self.identity)
        if last_probe_time is not None:
            elapsed = time.monotonic() - last_probe_time
            if elapsed < self.PROBE_COOLDOWN_SECONDS:
                logger.debug(
                    "ServiceCard {}/{}: probe skipped — cooldown active ({:.0f}s remaining)",
                    self.card_type,
                    self.identity,
                    self.PROBE_COOLDOWN_SECONDS - elapsed,
                )
                return self._last_result

        cmd = self.probe_command()
        logger.info("ServiceCard {}/{}: starting probe cmd={!r}", self.card_type, self.identity, cmd)
        try:
            session = self._session_manager.create_session(
                cmd,
                hub=None,
                container=None,  # Probes are one-shot; bypass tmux wrapping
            )
        except Exception:
            logger.exception("ServiceCard {}/{}: failed to create session", self.card_type, self.identity)
            return None

        # Wait for PTY to exit (session.alive is set False by _pty_read_loop on EOF)
        deadline = asyncio.get_running_loop().time() + self._probe_timeout
        while session.alive:
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(
                    "ServiceCard {}/{}: probe timed out after {}s",
                    self.card_type,
                    self.identity,
                    self._probe_timeout,
                )
                self._session_manager.destroy_session(session.session_id)
                return None
            await asyncio.sleep(0.5)

        # Read scrollback
        raw_output = session.scrollback.get_all().decode("utf-8", errors="replace")
        self._session_manager.destroy_session(session.session_id)

        # Parse
        try:
            result = self.parse_output(raw_output)
        except Exception:
            logger.exception(
                "ServiceCard {}/{}: parse_output raised, probe failed",
                self.card_type,
                self.identity,
            )
            return None

        self._last_result = result
        ServiceCard._probe_cooldowns[self.identity] = time.monotonic()
        logger.info(
            "ServiceCard {}/{}: probe succeeded, notifying {} subscriber(s)",
            self.card_type,
            self.identity,
            len(self._subscribers),
        )
        await self._notify_subscribers(result)
        return result

    async def _notify_subscribers(self, result: dict) -> None:
        """Notify all subscribers with the probe result. Fire-and-forget for async callbacks.

        Also emits a ``probe:{card_type}`` event on the EventBus if one is
        attached, so that any card or handler can react without a direct
        subscription to this ServiceCard instance.
        """
        for cb in list(self._subscribers):
            try:
                ret = cb(result)
                if inspect.isawaitable(ret):
                    task = asyncio.create_task(ret)
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)
            except Exception:
                logger.exception("ServiceCard {}/{}: subscriber callback raised", self.card_type, self.identity)

        # Emit on the EventBus (if wired)
        if self._bus is not None:
            await self._bus.emit(f"probe:{self.card_type}", result)

    async def start(self) -> None:
        """Run an initial probe immediately, then start the periodic probe loop."""
        # Run initial probe
        await self.run_probe()
        # Start periodic loop
        self._probe_task = asyncio.create_task(self._probe_loop())

    async def _probe_loop(self) -> None:
        """Periodic probe loop. Runs indefinitely until cancelled."""
        while True:
            try:
                await asyncio.sleep(self._interval_seconds)
                await self.run_probe()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("ServiceCard {}/{}: unexpected error in probe loop", self.card_type, self.identity)
                # Brief pause before retry to avoid tight error loops
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    break

    async def stop(self) -> None:
        """Cancel the probe loop and clean up."""
        if self._probe_task and not self._probe_task.done():
            self._probe_task.cancel()
            try:
                await self._probe_task
            except asyncio.CancelledError:
                pass
        self._probe_task = None
        # Cancel any in-flight subscriber notification tasks
        for task in list(self._pending_tasks):
            task.cancel()
        for task in list(self._pending_tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._pending_tasks.clear()
        logger.info("ServiceCard {}/{}: stopped", self.card_type, self.identity)
