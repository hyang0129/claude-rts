"""ServiceCardRegistry: singleton that manages ServiceCard instances with subscribe-or-reuse semantics."""

import inspect
from typing import Callable, Type
from loguru import logger
from .service_card import ServiceCard


class ServiceCardRegistry:
    """Server-level singleton registry for ServiceCard instances.

    Implements subscribe-or-reuse: multiple callers subscribing to the same
    (card_type, identity) key share one ServiceCard instance and probe loop.
    Cards are auto-stopped when their subscriber count drops to zero.
    """

    def __init__(self, session_manager):
        self._session_manager = session_manager
        self._cards: dict[str, ServiceCard] = {}
        self._factories: dict[str, Type[ServiceCard]] = {}

    def register_type(self, card_type: str, factory: Type[ServiceCard]) -> None:
        """Register a ServiceCard subclass for a given card_type string."""
        self._factories[card_type] = factory
        logger.debug("ServiceCardRegistry: registered type '{}'", card_type)

    async def subscribe(
        self,
        card_type: str,
        identity: str,
        callback: Callable,
        interval_seconds: int = 900,
        container: str | None = None,
    ) -> ServiceCard:
        """Subscribe a callback to a (card_type, identity) probe.

        If a card with that key already exists, registers the callback on it
        (no new card created). If not, creates a new card, starts it, and
        registers the callback.

        Returns the ServiceCard instance.
        """
        key = f"{card_type}:{identity}"

        if key in self._cards:
            card = self._cards[key]
            card.subscribe(callback)
            logger.info(
                "ServiceCardRegistry: reused existing card '{}', now {} subscriber(s)", key, card.subscriber_count
            )
            if card.last_result is not None:
                try:
                    ret = callback(card.last_result)
                    if inspect.isawaitable(ret):
                        import asyncio

                        task = asyncio.create_task(ret)
                        card._pending_tasks.add(task)
                        task.add_done_callback(card._pending_tasks.discard)
                except Exception:
                    logger.exception("ServiceCardRegistry: immediate last_result delivery to new subscriber raised")
            return card

        factory = self._factories.get(card_type)
        if factory is None:
            raise KeyError(f"Unknown ServiceCard type: {card_type!r}. Register it first with register_type().")

        card = factory(
            identity=identity,
            session_manager=self._session_manager,
            container=container,
            interval_seconds=interval_seconds,
        )
        card.subscribe(callback)
        self._cards[key] = card
        await card.start()
        logger.info("ServiceCardRegistry: created and started card '{}', {} subscriber(s)", key, card.subscriber_count)
        return card

    async def unsubscribe(self, card_type: str, identity: str, callback: Callable) -> None:
        """Unsubscribe a callback from a card. Auto-stops the card if no subscribers remain."""
        key = f"{card_type}:{identity}"
        card = self._cards.get(key)
        if card is None:
            return
        card.unsubscribe(callback)
        logger.debug("ServiceCardRegistry: unsubscribed from '{}', {} subscriber(s) remain", key, card.subscriber_count)
        if card.subscriber_count == 0:
            await card.stop()
            del self._cards[key]
            logger.info("ServiceCardRegistry: auto-stopped and removed card '{}' (no subscribers)", key)

    async def stop_all(self) -> None:
        """Stop all registered cards and clear the registry."""
        keys = list(self._cards.keys())
        for key in keys:
            card = self._cards.pop(key, None)
            if card:
                try:
                    await card.stop()
                except Exception:
                    logger.exception("ServiceCardRegistry: error stopping card '{}'", key)
        logger.info("ServiceCardRegistry: all {} card(s) stopped", len(keys))

    def get(self, card_type: str, identity: str) -> ServiceCard | None:
        """Look up a card by (card_type, identity) without subscribing."""
        return self._cards.get(f"{card_type}:{identity}")
