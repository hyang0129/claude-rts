"""EventBus: central pub/sub for decoupled cross-card communication."""

import asyncio
import inspect
from typing import Callable

from loguru import logger


class EventBus:
    """Async-friendly pub/sub bus for server-side card communication.

    Events are ``(event_type: str, payload: dict)``.  Callbacks may be sync
    or async — async callbacks are dispatched via ``asyncio.create_task``
    (fire-and-forget).

    Wildcard subscribers (``subscribe("*", cb)``) receive every event.
    Subscriber exceptions are logged and never propagate to the emitter.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable]] = {}
        self._pending_tasks: set[asyncio.Task] = set()

    def subscribe(self, event_type: str, callback: Callable) -> None:
        """Register *callback* for *event_type* (or ``"*"`` for all events)."""
        subs = self._subscribers.setdefault(event_type, [])
        if callback not in subs:
            subs.append(callback)
            logger.debug("EventBus: subscribed to '{}' ({} total)", event_type, len(subs))

    def unsubscribe(self, event_type: str, callback: Callable) -> None:
        """Remove a previously registered callback.  No-op if not found."""
        subs = self._subscribers.get(event_type)
        if subs is None:
            return
        try:
            subs.remove(callback)
            logger.debug("EventBus: unsubscribed from '{}' ({} remain)", event_type, len(subs))
        except ValueError:
            pass

    async def emit(self, event_type: str, payload: dict) -> None:
        """Fan out *payload* to all subscribers of *event_type* and wildcard ``"*"``.

        Sync callbacks are called directly; async callbacks are scheduled as
        fire-and-forget tasks.  Exceptions in any callback are logged and do
        not prevent delivery to remaining subscribers.
        """
        targets: list[Callable] = []
        targets.extend(self._subscribers.get(event_type, []))
        if event_type != "*":
            targets.extend(self._subscribers.get("*", []))

        for cb in list(targets):
            try:
                ret = cb(event_type, payload)
                if inspect.isawaitable(ret):
                    task = asyncio.create_task(ret)
                    self._pending_tasks.add(task)
                    task.add_done_callback(lambda t: self._task_done(t, event_type))
            except Exception:
                logger.exception(
                    "EventBus: subscriber raised for event '{}'",
                    event_type,
                )

    def _task_done(self, task: asyncio.Task, event_type: str) -> None:
        """Done-callback for fire-and-forget async subscriber tasks."""
        self._pending_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "EventBus: async subscriber raised for event '{}': {}",
                    event_type,
                    exc,
                )

    def clear(self) -> None:
        """Remove all subscriptions and cancel pending async tasks."""
        self._subscribers.clear()
        for task in list(self._pending_tasks):
            task.cancel()
        self._pending_tasks.clear()
        logger.info("EventBus: cleared all subscriptions")
