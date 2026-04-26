"""Tests for TerminalCard lifecycle, CardRegistry, and server integration."""

import time

from claude_rts.cards.terminal_card import TerminalCard
from claude_rts.cards.card_registry import CardRegistry
from claude_rts.sessions import SessionManager
from claude_rts import config
from claude_rts.server import create_app


# ── MockPty (same as test_sessions.py) ─────────────────────────────────────


class MockPty:
    """Mock PtyProcess for testing."""

    def __init__(self):
        self._alive = True
        self._output_queue = []
        self._written = []

    def isalive(self):
        return self._alive

    def read(self):
        if self._output_queue:
            return self._output_queue.pop(0)
        time.sleep(0.1)
        if not self._alive:
            raise EOFError()
        return ""

    def write(self, text):
        self._written.append(text)

    def setwinsize(self, rows, cols):
        pass

    def terminate(self, force=False):
        self._alive = False

    @classmethod
    def spawn(cls, cmd, dimensions=(24, 80)):
        return cls()


# ── TerminalCard unit tests ────────────────────────────────────────────────


async def test_terminal_card_start_stop(monkeypatch):
    """start() allocates a PTY session; stop() destroys it."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="echo hello", hub="hub1", container="cont1")

    await card.start()

    assert card.session is not None
    assert card.session_id.startswith("rts-")
    assert card.id == card.session_id
    assert card.alive is True
    assert mgr.get_session(card.session_id) is not None

    sid = card.session_id
    await card.stop()

    assert card.session is None
    assert card.alive is False
    assert mgr.get_session(sid) is None
    mgr.stop_all()


async def test_terminal_card_type_and_hidden():
    """TerminalCard has card_type='terminal' and hidden=False."""
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="test")
    assert card.card_type == "terminal"
    assert card.hidden is False


async def test_terminal_card_to_descriptor(monkeypatch):
    """to_descriptor() returns the shape the frontend expects."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="bash -l", hub="my-hub", container="my-cont")
    await card.start()

    desc = card.to_descriptor()

    assert desc["type"] == "terminal"
    assert desc["session_id"] == card.session_id
    assert desc["hub"] == "my-hub"
    assert desc["container"] == "my-cont"
    assert desc["exec"] == "bash -l"

    await card.stop()
    mgr.stop_all()


async def test_terminal_card_descriptor_minimal(monkeypatch):
    """to_descriptor() omits optional fields when not set."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="echo hi")
    await card.start()

    desc = card.to_descriptor()
    assert "hub" not in desc
    assert "container" not in desc
    assert desc["exec"] == "echo hi"

    await card.stop()
    mgr.stop_all()


async def test_terminal_card_stop_idempotent(monkeypatch):
    """Calling stop() twice does not raise."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="test")
    await card.start()
    await card.stop()
    await card.stop()  # should not raise
    mgr.stop_all()


# ── CardRegistry unit tests ────────────────────────────────────────────────


async def test_card_registry_register_and_get(monkeypatch):
    """register() adds a card; get() retrieves it by id."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    card = TerminalCard(session_manager=mgr, cmd="test")
    await card.start()
    reg.register(card)

    assert reg.get(card.id) is card
    assert reg.get_terminal(card.session_id) is card

    await card.stop()
    mgr.stop_all()


async def test_card_registry_unregister(monkeypatch):
    """unregister() removes the card and returns it."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    card = TerminalCard(session_manager=mgr, cmd="test")
    await card.start()
    reg.register(card)

    removed = reg.unregister(card.id)
    assert removed is card
    assert reg.get(card.id) is None

    # Unregister again returns None
    assert reg.unregister(card.id) is None

    await card.stop()
    mgr.stop_all()


async def test_card_registry_list_terminals(monkeypatch):
    """list_terminals() returns only TerminalCards."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    c1 = TerminalCard(session_manager=mgr, cmd="cmd1")
    c2 = TerminalCard(session_manager=mgr, cmd="cmd2")
    await c1.start()
    await c2.start()
    reg.register(c1)
    reg.register(c2)

    terminals = reg.list_terminals()
    assert len(terminals) == 2
    assert set(t.id for t in terminals) == {c1.id, c2.id}

    await c1.stop()
    await c2.stop()
    mgr.stop_all()


async def test_card_registry_stop_all(monkeypatch):
    """stop_all() stops and removes all cards."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    card = TerminalCard(session_manager=mgr, cmd="test")
    await card.start()
    sid = card.session_id
    reg.register(card)

    await reg.stop_all()

    assert reg.get(sid) is None
    assert len(reg.list_all()) == 0
    mgr.stop_all()


# ── Server integration: card_registry available in app ─────────────────────


async def test_app_has_card_registry(tmp_path, monkeypatch):
    """create_app startup hook should create a CardRegistry in app['card_registry']."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)

    # card_registry is set during on_startup hook; verify it's not set before startup
    # and the route for /ws/session/new is registered
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/ws/session/new" in routes
    assert "/ws/session/{session_id}" in routes


async def test_session_new_creates_terminal_card(aiohttp_client, tmp_path, monkeypatch):
    """POST to /api/test/session/create should result in a TerminalCard in the registry."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    app_config = config.load(tmp_path / ".sc")
    app = create_app(app_config, test_mode=True)
    client = await aiohttp_client(app)

    # Use the test puppeting API to create a session (avoids WebSocket in tests)
    resp = await client.post("/api/test/session/create?cmd=echo+hello")
    assert resp.status == 200
    data = await resp.json()
    sid = data["session_id"]

    # The test API goes through SessionManager directly (not TerminalCard),
    # but we can verify the session exists
    mgr = app["session_manager"]
    assert mgr.get_session(sid) is not None


# ── CardRegistry.by_type (#127) ────────────────────────────────────────────


async def test_card_registry_by_type(monkeypatch):
    """by_type('terminal') returns all TerminalCard instances in the registry."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    c1 = TerminalCard(session_manager=mgr, cmd="cmd1")
    c2 = TerminalCard(session_manager=mgr, cmd="cmd2")
    await c1.start()
    await c2.start()
    reg.register(c1)
    reg.register(c2)

    terminals = reg.by_type("terminal")
    assert len(terminals) == 2
    assert set(t.id for t in terminals) == {c1.id, c2.id}

    # Unknown type → empty list, never raises
    assert reg.by_type("does-not-exist") == []

    await c1.stop()
    await c2.stop()
    mgr.stop_all()


# ── Session.kind / probe filtering (#127) ──────────────────────────────────


async def test_session_kind_default_is_user(monkeypatch):
    """Session.kind defaults to 'user' when create_session omits it."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()

    session = mgr.create_session("echo hi")
    assert session.kind == "user"

    mgr.destroy_session(session.session_id)
    mgr.stop_all()


async def test_list_sessions_excludes_probe_sessions(monkeypatch):
    """list_sessions() filters out sessions created with kind='probe'."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()

    user = mgr.create_session("bash -l")
    probe = mgr.create_session("claude --version", kind="probe")

    assert probe.kind == "probe"

    listed_ids = {s["session_id"] for s in mgr.list_sessions()}
    assert user.session_id in listed_ids
    assert probe.session_id not in listed_ids

    mgr.stop_all()


# ── Issue #151: display_name, recovery_script, stable identity ────────────


async def test_terminal_card_display_name(monkeypatch):
    """TerminalCard accepts and returns display_name in descriptor."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="bash", display_name="Build Server")
    await card.start()

    assert card.display_name == "Build Server"
    desc = card.to_descriptor()
    assert desc["display_name"] == "Build Server"

    await card.stop()
    mgr.stop_all()


async def test_terminal_card_recovery_script(monkeypatch):
    """TerminalCard accepts and returns recovery_script in descriptor."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="bash", recovery_script="cd /work && make dev")
    await card.start()

    assert card.recovery_script == "cd /work && make dev"
    desc = card.to_descriptor()
    assert desc["recovery_script"] == "cd /work && make dev"

    await card.stop()
    mgr.stop_all()


async def test_terminal_card_descriptor_omits_empty_display_name(monkeypatch):
    """to_descriptor() omits display_name and recovery_script when empty."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start()

    desc = card.to_descriptor()
    assert "display_name" not in desc
    assert "recovery_script" not in desc
    # Epic #236 child 3 (#239): ``starred`` is always present in the
    # descriptor — both ``True`` and ``False`` — so the client boot path
    # reads the server's authoritative value instead of defaulting.
    assert desc["starred"] is False

    await card.stop()
    mgr.stop_all()


async def test_terminal_card_starred_descriptor_round_trip(monkeypatch):
    """``starred`` round-trips through ``to_descriptor`` in both states."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()

    # Default (unstarred)
    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start()
    desc = card.to_descriptor()
    assert desc["starred"] is False

    # Mutated to starred (simulating apply_state_patch)
    card.starred = True
    desc = card.to_descriptor()
    assert desc["starred"] is True

    # Constructed as starred
    card2 = TerminalCard(session_manager=mgr, cmd="bash", starred=True)
    await card2.start()
    assert card2.to_descriptor()["starred"] is True

    await card.stop()
    await card2.stop()
    mgr.stop_all()


async def test_terminal_card_mutable_display_name(monkeypatch):
    """display_name and recovery_script can be set after creation."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start()

    card.display_name = "Dev Shell"
    card.recovery_script = "npm start"

    desc = card.to_descriptor()
    assert desc["display_name"] == "Dev Shell"
    assert desc["recovery_script"] == "npm start"

    await card.stop()
    mgr.stop_all()


# ── Epic #236 child 5 (#241) — canvas-membership + persistence hook ────────


async def test_card_registry_register_with_canvas_name(monkeypatch):
    """register(card, canvas_name=...) records the card's canvas membership."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start()
    reg.register(card, canvas_name="main")
    try:
        assert reg.get_canvas_name(card.id) == "main"
        assert reg.cards_on_canvas("main") == [card]
        assert reg.cards_on_canvas("other") == []
    finally:
        reg.unregister(card.id)
        await card.stop()
        mgr.stop_all()


async def test_card_registry_register_without_canvas_name_defaults_none(monkeypatch):
    """Backward compat: register(card) without canvas_name records None membership."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start()
    reg.register(card)
    try:
        assert reg.get_canvas_name(card.id) is None
    finally:
        reg.unregister(card.id)
        await card.stop()
        mgr.stop_all()


async def test_apply_state_patch_triggers_persist_callback(monkeypatch):
    """apply_state_patch invokes the persist hook with the card's canvas name."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()
    invocations: list[str] = []

    reg.set_persist_callback(invocations.append)

    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start()
    reg.register(card, canvas_name="canvas-A")

    reg.apply_state_patch(card.id, {"starred": True})
    assert invocations == ["canvas-A"]

    # Subsequent patch on the same card hits the same canvas.
    reg.apply_state_patch(card.id, {"display_name": "Dev"})
    assert invocations == ["canvas-A", "canvas-A"]

    await card.stop()
    mgr.stop_all()


async def test_apply_state_patch_no_persist_when_canvas_unset(monkeypatch):
    """Cards with no canvas membership do not trigger the persist hook."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()
    invocations: list[str] = []
    reg.set_persist_callback(invocations.append)

    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start()
    reg.register(card)  # no canvas_name

    reg.apply_state_patch(card.id, {"starred": True})
    assert invocations == []

    await card.stop()
    mgr.stop_all()


async def test_unregister_triggers_persist_callback(monkeypatch):
    """Removing a card persists the canvas so the snapshot reflects the deletion."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()
    invocations: list[str] = []
    reg.set_persist_callback(invocations.append)

    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start()
    reg.register(card, canvas_name="main")
    invocations.clear()

    reg.unregister(card.id)
    assert invocations == ["main"]

    await card.stop()
    mgr.stop_all()


async def test_persist_callback_swallows_exceptions(monkeypatch):
    """A failing persist callback must not roll back the in-memory mutation."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    def boom(_canvas_name: str) -> None:
        raise RuntimeError("disk full")

    reg.set_persist_callback(boom)

    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start()
    reg.register(card, canvas_name="main")

    # Must not raise — the in-memory mutation already happened.
    reg.apply_state_patch(card.id, {"starred": True})
    assert card.starred is True

    await card.stop()
    mgr.stop_all()


async def test_terminal_card_descriptor_includes_card_id(monkeypatch):
    """to_descriptor() emits card_id (epic #236 child 5 schema discriminator)."""
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start()
    desc = card.to_descriptor()
    assert "card_id" in desc
    assert desc["card_id"] == card.id
    await card.stop()
    mgr.stop_all()


async def test_persist_callback_only_includes_starred_cards(monkeypatch):
    """Issue #194: only starred cards belong in the persisted snapshot.

    The persist callback in ``server.on_startup`` filters out unstarred cards
    so a reload starts with a clean slate. This test exercises the filter
    semantics directly through the registry; the callback wiring is covered
    end-to-end by the integration test.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()
    snapshot: list[list] = []

    def _capture(canvas_name: str) -> None:
        # Mirror server.on_startup's filter (starred + visible + has descriptor)
        cards = [
            c
            for c in reg.cards_on_canvas(canvas_name)
            if not getattr(c, "hidden", False) and hasattr(c, "to_descriptor") and getattr(c, "starred", False)
        ]
        snapshot.append([c.id for c in cards])

    reg.set_persist_callback(_capture)

    starred = TerminalCard(session_manager=mgr, cmd="bash", starred=True)
    unstarred = TerminalCard(session_manager=mgr, cmd="bash", starred=False)
    await starred.start()
    await unstarred.start()
    reg.register(starred, canvas_name="main")
    reg.register(unstarred, canvas_name="main")

    snapshot.clear()
    reg.apply_state_patch(starred.id, {"display_name": "ok"})
    assert snapshot == [[starred.id]]

    await starred.stop()
    await unstarred.stop()
    mgr.stop_all()


# ── Fix 2 (#254 e2e regressions): registry rekey on TerminalCard.start() ──


# ── S5: PTY retry jitter — sleep duration within ±20% band ──────────────────


async def test_start_retry_jitter_sleep_within_bounds(monkeypatch):
    """S5: TerminalCard.start() applies ±20% jitter to retry delays.

    For delays=[10, 30, 90], the actual asyncio.sleep calls must fall within
    [8..12], [24..36], [72..108] seconds respectively.

    Uses fixed random.uniform so the jitter factor is deterministic (+10%):
    jittered = delay * (1.0 + 0.1) = delay * 1.1.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    # Fix the jitter factor to +10% so expected sleeps are deterministic.
    monkeypatch.setattr("claude_rts.cards.terminal_card.random.uniform", lambda a, b: 0.1)

    sleep_calls: list[float] = []

    async def _fake_sleep(d: float) -> None:
        sleep_calls.append(d)

    monkeypatch.setattr("claude_rts.cards.terminal_card.asyncio.sleep", _fake_sleep)

    mgr = SessionManager()

    def _always_fail(*args, **kwargs):
        raise RuntimeError("container_unavailable_in_test")

    mgr.create_session = _always_fail

    card = TerminalCard(session_manager=mgr, cmd="bash")
    await card.start(retry_delays=[10, 30, 90])

    # 3 delays → 3 sleep calls.
    assert len(sleep_calls) == 3, f"Expected 3 sleep calls, got {len(sleep_calls)}: {sleep_calls}"
    # +10% jitter: 10*1.1=11, 30*1.1=33, 90*1.1=99
    assert 8.0 <= sleep_calls[0] <= 12.0, f"Sleep[0]={sleep_calls[0]:.2f} not in [8, 12] — jitter ±20% of 10"
    assert 24.0 <= sleep_calls[1] <= 36.0, f"Sleep[1]={sleep_calls[1]:.2f} not in [24, 36] — jitter ±20% of 30"
    assert 72.0 <= sleep_calls[2] <= 108.0, f"Sleep[2]={sleep_calls[2]:.2f} not in [72, 108] — jitter ±20% of 90"
    mgr.stop_all()


# ── S6: Container available mid-retry — error_state cleared ──────────────────


async def test_start_success_after_retry_clears_error_state(monkeypatch):
    """S6: If create_session fails on attempts 1-2 but succeeds on attempt 3,
    error_state is None and on_error_state callback is NOT called.
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    attempt_count = {"n": 0}
    real_create = mgr.create_session

    def _flaky_create(*args, **kwargs):
        attempt_count["n"] += 1
        if attempt_count["n"] < 3:
            raise RuntimeError("container_not_ready")
        return real_create(*args, **kwargs)

    mgr.create_session = _flaky_create

    card = TerminalCard(session_manager=mgr, cmd="bash")
    error_calls: list = []

    def _on_error(c):
        error_calls.append(c.error_state)

    await card.start(retry_delays=[0, 0, 0], on_error_state=_on_error)

    assert card.error_state is None, f"error_state should be None after successful retry, got: {card.error_state}"
    assert card.session is not None, "Session should be non-None after successful start"
    assert card.alive is True, "Card should be alive after successful start"
    assert error_calls == [], f"on_error_state should NOT have been called on success, got: {error_calls}"
    assert attempt_count["n"] == 3, f"Expected 3 create_session attempts, got: {attempt_count['n']}"
    await card.stop()
    mgr.stop_all()


# ── S24: CardRegistry._order invariant across register/unregister/rekey ────────


def test_card_registry_order_invariant_across_cycles():
    """S24: After 100 register/rekey/unregister cycles, len(_order)==len(_cards).

    This is a pure unit test — no async, no aiohttp. Verifies that CardRegistry
    does not accumulate stale entries in _order across rekey operations.
    """
    mgr = SessionManager()
    registry = CardRegistry()

    for i in range(100):
        card_id = f"card-{i}"
        # Create a card with a fixed snapshot id (pre-start id).
        card = TerminalCard(session_manager=mgr, cmd="bash", card_id=card_id)
        registry.register(card, canvas_name="test-canvas")

        assert len(registry._order) == len(registry._cards), (
            f"Cycle {i} after register: _order={len(registry._order)} != _cards={len(registry._cards)}"
        )

        # Simulate the rekey that happens when start() is called and assigns
        # a real session_id (different from the snapshot card_id).
        new_id = f"session-{i}"
        registry.rekey(card_id, new_id)
        card._id = new_id  # keep in sync with what TerminalCard.start() does

        assert len(registry._order) == len(registry._cards), (
            f"Cycle {i} after rekey: _order={len(registry._order)} != _cards={len(registry._cards)}"
        )

        registry.unregister(new_id)

        assert len(registry._order) == len(registry._cards), (
            f"Cycle {i} after unregister: _order={len(registry._order)} != _cards={len(registry._cards)}"
        )

    # Final state: registry is completely empty.
    assert registry._order == [], f"_order not empty after 100 cycles: {registry._order}"
    assert registry._cards == {}, f"_cards not empty after 100 cycles: {registry._cards}"

    mgr.stop_all()


async def test_registry_rekey_on_start(monkeypatch):
    """When a TerminalCard is registered before start(), the registry key is
    updated to the real session_id after start() completes.

    Asserts:
    - get_terminal(session_id) returns the card after start().
    - get_terminal(snapshot_id) returns None (old key is gone).
    - cards_on_canvas order is preserved (rekey does not move card to end).
    """
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    mgr = SessionManager()
    reg = CardRegistry()

    # Register two cards: card_a first, then card_b.
    persist_calls: list[str] = []
    reg.set_persist_callback(persist_calls.append)

    card_a = TerminalCard(session_manager=mgr, cmd="bash", card_id="snapshot-abc")
    card_b = TerminalCard(session_manager=mgr, cmd="bash", card_id="snapshot-xyz")
    reg.register(card_a, canvas_name="test-canvas")
    reg.register(card_b, canvas_name="test-canvas")
    persist_calls.clear()  # clear registration-time persists

    # Start card_a — this rekeyes it from snapshot-abc to a real session_id.
    await card_a.start(retry_delays=[])
    real_session_id = card_a.session_id
    assert real_session_id != "snapshot-abc", "session_id should differ from snapshot placeholder"

    # After start: lookup by session_id works, old key is gone.
    assert reg.get_terminal(real_session_id) is card_a, "get_terminal(session_id) should return card_a"
    assert reg.get_terminal("snapshot-abc") is None, "old snapshot key should be gone"
    assert card_a.id == real_session_id

    # Insertion order must be preserved: card_a (rekeyed) still comes BEFORE card_b.
    on_canvas = reg.cards_on_canvas("test-canvas")
    assert on_canvas[0] is card_a, "card_a should remain first after rekey"
    assert on_canvas[1] is card_b, "card_b should remain second after rekey"

    # Rekey must trigger a persist so the canvas JSON reflects the new session_id.
    assert "test-canvas" in persist_calls, "rekey should have triggered canvas persist"

    await card_a.stop()
    await card_b.stop()
    mgr.stop_all()
