"""WidgetCard: first-class server-side card for info-widgets (system-info,
container-manager, profiles, claude-usage, …).

Epic #254 child 5 (#260): widgets previously existed only as a client-side
JavaScript class in ``static/index.html``. Canvas JSON snapshots contained
widget entries but no server-side Python object was ever created — so the
server's ``CardRegistry`` had no awareness of widgets, and the boot-time
hydration path introduced by child 2 (#257) skipped ``type=="widget"`` entries
with a debug log.

This class closes the gap by providing a minimal server-side representation
that participates in the same hydration / registry / ``to_descriptor`` contract
as ``TerminalCard``. Widgets have no PTY and no background work — ``start()``
and ``stop()`` are no-ops — but existence, ``starred``, position / size, and
the discriminating ``widget_type`` field are authoritative on the server.
"""

from __future__ import annotations

from .base import BaseCard


class WidgetCard(BaseCard):
    """Server-side widget card.

    Widgets have no PTY and no background work — they render client-side by
    polling the corresponding ``/api/widgets/{type}`` data endpoint at their
    own refresh interval. The server only owns existence + position / size /
    ``starred``, exposed through ``to_descriptor`` for the hydration / boot
    response and ``card_updated`` broadcast.
    """

    card_type: str = "widget"
    hidden: bool = False

    MUTABLE_FIELDS: frozenset[str] = frozenset({"starred", "x", "y", "w", "h", "z_order"})
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
        widget_type: str,
        card_id: str | None = None,
        layout: dict | None = None,
        starred: bool = False,
        refresh_interval: int | None = None,
    ):
        super().__init__(card_id=card_id)
        if not isinstance(widget_type, str) or not widget_type:
            raise ValueError("WidgetCard.widget_type must be a non-empty string")
        # Back-compat: legacy 'vm-manager' widget type renamed to
        # 'container-manager' (#201). Mirror the client's fix in
        # ``WidgetCard.fromSerialized`` so server-side hydration of an old
        # snapshot yields the current widget type.
        if widget_type == "vm-manager":
            widget_type = "container-manager"
        self.widget_type = widget_type
        self.starred = bool(starred)
        self.refresh_interval = (
            int(refresh_interval)
            if isinstance(refresh_interval, int) and not isinstance(refresh_interval, bool)
            else None
        )

        self.layout = layout or {}
        self._explicit_geometry: set[str] = set()
        if self.layout:
            for _field in ("x", "y", "w", "h", "z_order"):
                _val = self.layout.get(_field)
                if isinstance(_val, int) and not isinstance(_val, bool):
                    setattr(self, _field, _val)
                    self._explicit_geometry.add(_field)

    def to_descriptor(self) -> dict:
        desc: dict = {
            "type": self.card_type,
            "card_id": self.id,
            "widgetType": self.widget_type,
            "starred": bool(self.starred),
        }
        if self.refresh_interval is not None:
            desc["refreshInterval"] = self.refresh_interval
        for _field in ("x", "y", "w", "h", "z_order"):
            if _field in self._explicit_geometry:
                desc[_field] = int(getattr(self, _field))
        return desc

    async def start(self, **_kwargs) -> None:  # type: ignore[override]
        """No-op — widgets have no background activity.

        Accepts ``**_kwargs`` so ``hydrate_canvas_into_registry`` can pass
        ``retry_delays`` / ``on_error_state`` uniformly without a TypeError.
        """
        return None

    async def stop(self) -> None:  # type: ignore[override]
        return None

    @classmethod
    def from_descriptor(cls, data: dict, **kwargs) -> "WidgetCard":
        widget_type = data.get("widgetType") or data.get("widget_type")
        if not isinstance(widget_type, str) or not widget_type:
            raise ValueError("WidgetCard.from_descriptor: entry missing 'widgetType'")
        layout = {
            k: data[k]
            for k in ("x", "y", "w", "h", "z_order")
            if isinstance(data.get(k), int) and not isinstance(data.get(k), bool)
        }
        refresh_interval = data.get("refreshInterval") or data.get("refresh_interval")
        return cls(
            widget_type=widget_type,
            card_id=data.get("card_id") or None,
            layout=layout or None,
            starred=bool(data.get("starred", False)),
            refresh_interval=refresh_interval
            if isinstance(refresh_interval, int) and not isinstance(refresh_interval, bool)
            else None,
        )
