"""Playwright E2E tests for PR #268 (epic #254) — Bucket B boot/spawn scenarios.

Covers:
  S1  — Cold boot: no /ws/session/new for boot-hydrated terminals
  S2  — Warm boot (server restart): browser reconnect does not duplicate cards
  S11 — Canvas switch: no /ws/session/new for already-registered cards
  S13 — User-initiated terminal spawn creates a dormant card (no WS opened)
  S19 — Per-device fields (pan, zoom, controlGroups) absent from canvas JSON snapshot
"""

import os
import subprocess
import sys
import tempfile
import time

import pytest

# Skip entire module if playwright is not installed
pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright  # noqa: E402

from tests.e2e.conftest import _wait_for_server, open_context_menu  # noqa: E402


# ── Module-level preset override ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def dev_config_preset():
    """Use stress-test preset — has 4 starred terminal cards at boot."""
    return "stress-test"


@pytest.fixture(scope="module")
def dev_config_preset_canvas_switch():
    """Use canvas-switch-test preset — two canvases (canvas-a, canvas-b) for S11."""
    return "canvas-switch-test"


# ── S1: Cold boot — no /ws/session/new for boot-hydrated terminals ────────────


def test_cold_boot_no_session_new_for_hydrated_cards(backend_server, backend_port):
    """S1: On first browser load, /ws/session/new is never opened for hydrated terminals.

    Hydrated terminals are attached via /ws/session/{id} (attach-only).
    Only /ws/control and /ws/session/{id} paths are valid during boot.
    """
    headed = os.environ.get("HEADED", "").lower() in ("1", "true")
    websocket_urls: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        pg = browser.new_page()

        # Collect all WebSocket URLs opened before boot completes.
        pg.on("websocket", lambda ws: websocket_urls.append(ws.url))

        pg.goto(f"http://localhost:{backend_port}")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_selector("#canvas", timeout=15000)
        pg.wait_for_function(
            "() => window.__claudeRtsBootComplete === true",
            timeout=15000,
        )

        pg.close()
        browser.close()

    session_new_opens = [url for url in websocket_urls if "/ws/session/new" in url]
    assert not session_new_opens, (
        f"Unexpected /ws/session/new opens during cold boot: {session_new_opens}\nAll WS URLs: {websocket_urls}"
    )


# ── S2: Warm boot — server restart does not duplicate cards ───────────────────


def test_warm_boot_no_duplicate_cards(backend_port, dev_config_preset):
    """S2: After server restart, browser reconnect renders without duplicate card IDs.

    Uses a local subprocess to avoid mutating the module-scoped backend_server
    fixture. Spawns a fresh server, connects a browser, restarts the server,
    reloads the browser, and asserts no card_id appears more than once.
    """
    headed = os.environ.get("HEADED", "").lower() in ("1", "true")
    env = os.environ.copy()
    env["CLAUDE_RTS_TEST_MODE"] = "1"

    # Use a different port to avoid colliding with the module-scoped backend_server.
    warm_port = backend_port + 50

    stdout_f = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, prefix="warm-stdout-")
    stderr_f = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, prefix="warm-stderr-")

    def _start_proc():
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "claude_rts",
                "--port",
                str(warm_port),
                "--no-browser",
                "--dev-config",
                dev_config_preset,
                "--test-mode",
            ],
            env=env,
            stdout=stdout_f,
            stderr=stderr_f,
        )

    proc = _start_proc()
    try:
        if not _wait_for_server(warm_port, timeout=30.0):
            proc.terminate()
            pytest.skip("Warm-boot test server did not start — skipping S2")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not headed)
            pg = browser.new_page()
            pg.goto(f"http://localhost:{warm_port}")
            pg.wait_for_load_state("networkidle")
            pg.wait_for_selector("#canvas", timeout=15000)
            pg.wait_for_function("() => window.__claudeRtsBootComplete === true", timeout=15000)

            # Record initial stable card UIDs (cardUid persists across server restarts;
            # sessionId is ephemeral — the PTY session is recreated on each restart).
            initial_ids = pg.evaluate("() => cards.map(c => c.cardUid || c.cardId).filter(Boolean)")

            # Restart the server.
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

            # Brief pause to ensure port is freed.
            proc = _start_proc()
            if not _wait_for_server(warm_port, timeout=30.0):
                pytest.skip("Warm-boot restart server did not come back — skipping S2")

            # Reload the browser page so the client reconnects to the restarted server.
            pg.reload()
            pg.wait_for_load_state("networkidle")
            pg.wait_for_selector("#canvas", timeout=15000)
            pg.wait_for_function("() => window.__claudeRtsBootComplete === true", timeout=15000)

            card_ids = pg.evaluate("() => cards.map(c => c.cardUid || c.cardId).filter(Boolean)")

            pg.close()
            browser.close()

        # Each individual ID must be unique (no duplicates within the reload).
        assert len(card_ids) == len(set(card_ids)), (
            f"Duplicate card IDs found after warm boot: {card_ids}\nInitial IDs were: {initial_ids}"
        )
        # The set of card IDs must match the pre-reload set (re-attachment identity,
        # not just uniqueness). A regression that re-creates sessions instead of
        # attaching would produce a different set of IDs.
        assert set(card_ids) == set(initial_ids), (
            f"Card IDs changed across warm boot — server re-created sessions instead of re-attaching.\n"
            f"Before restart: {sorted(initial_ids)}\n"
            f"After restart:  {sorted(card_ids)}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        for f in (stdout_f, stderr_f):
            try:
                f.close()
                os.unlink(f.name)
            except OSError:
                pass


# ── S11: Canvas switch does not fire /ws/session/new for registered cards ─────


def test_canvas_switch_no_session_new_for_registered_cards(backend_port, dev_config_preset_canvas_switch):
    """S11: Switching between two hydrated canvases never opens /ws/session/new.

    Uses the canvas-switch-test preset which has two canvases (canvas-a and
    canvas-b) both hydrated at boot under keep_resident policy. Switching
    from canvas-a to canvas-b must use the attach path (/ws/session/{id})
    — never /ws/session/new — because the cards on canvas-b were already
    registered by the server at startup.
    """
    headed = os.environ.get("HEADED", "").lower() in ("1", "true")
    env = os.environ.copy()
    env["CLAUDE_RTS_TEST_MODE"] = "1"

    switch_port = backend_port + 80  # distinct from module-scoped server

    stdout_f = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, prefix="s11-stdout-")
    stderr_f = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, prefix="s11-stderr-")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "claude_rts",
            "--port",
            str(switch_port),
            "--no-browser",
            "--dev-config",
            dev_config_preset_canvas_switch,
            "--test-mode",
        ],
        env=env,
        stdout=stdout_f,
        stderr=stderr_f,
    )
    try:
        if not _wait_for_server(switch_port, timeout=30.0):
            proc.terminate()
            pytest.skip("canvas-switch-test server did not start — skipping S11")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not headed)
            pg = browser.new_page()

            # Collect only the WS opens that happen AFTER boot.
            switch_ws_opens: list[str] = []

            pg.goto(f"http://localhost:{switch_port}")
            pg.wait_for_load_state("networkidle")
            pg.wait_for_selector("#canvas", timeout=15000)
            pg.wait_for_function("() => window.__claudeRtsBootComplete === true", timeout=15000)

            # Start collecting AFTER boot so boot-time attaches are excluded.
            pg.on("websocket", lambda ws: switch_ws_opens.append(ws.url))

            # Switch from canvas-a (default) to canvas-b. Both canvases are hydrated
            # at boot under keep_resident — no /ws/session/new should open.
            pg.evaluate(
                "async () => {"
                "  if (typeof switchCanvas === 'function') {"
                "    await switchCanvas('canvas-b');"
                "  }"
                "}"
            )
            time.sleep(0.5)  # brief wait for any WS opens to be captured

            # Toothlessness guard: if canvas-b rendered 0 cards, the
            # "no /ws/session/new" assertion below would hold vacuously
            # (switchCanvas's render loop never iterates). Fail loudly so
            # a broken fixture (e.g. a card that hub-filters out of
            # /api/cards) cannot silently disarm this test.
            #
            # Poll briefly for the renderer to settle — switchCanvas's render
            # loop is async (per-descriptor await), so a flake on the 0.5s
            # sleep above shouldn't be allowed to wrongly fail the guard.
            try:
                pg.wait_for_function(
                    "() => (typeof cards !== 'undefined' && cards.length > 0)",
                    timeout=5000,
                )
            except Exception:
                pass  # fall through to the explicit assertion below
            rendered_count = pg.evaluate("() => (typeof cards !== 'undefined' ? cards.length : 0)")
            descriptors = pg.evaluate(
                "async () => {"
                "  const r = await fetch('/api/cards?canvas=canvas-b');"
                "  if (!r.ok) return [];"
                "  return await r.json();"
                "}"
            )
            assert rendered_count > 0 and len(descriptors) > 0, (
                f"Toothlessness guard: canvas-b rendered {rendered_count} cards "
                f"and /api/cards?canvas=canvas-b returned {len(descriptors)} "
                f"descriptors. Both must be > 0 for this test to be meaningful "
                f"— otherwise the 'no /ws/session/new' assertion holds vacuously."
            )

            session_new_opens = [url for url in switch_ws_opens if "/ws/session/new" in url]
            assert session_new_opens == [], (
                f"Unexpected /ws/session/new during canvas switch: {session_new_opens}\n"
                f"All WS URLs after boot: {switch_ws_opens}"
            )

            pg.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        for f in (stdout_f, stderr_f):
            try:
                f.close()
                os.unlink(f.name)
            except OSError:
                pass


# ── S13: User-initiated terminal spawn creates a dormant card ─────────────────


def test_user_spawn_creates_dormant_card(backend_server, backend_port):
    """S13 (epic #254): Context-menu spawn creates a dormant card; NO WS opens.

    Per epic #254's dormant-by-default model, ``CARD_TYPE_REGISTRY.spawn(
    'terminal', { hub, x, y })`` produces a card with ``starred=false``.
    ``TerminalCard.onInit()`` then short-circuits to ``_renderDormant()``
    without calling ``_activate()`` / ``connectWs()``. The correct invariant
    is therefore that NO ``/ws/session/new`` WebSocket is opened at spawn
    time, and the new card appears in dormant state.
    """
    headed = os.environ.get("HEADED", "").lower() in ("1", "true")
    session_new_urls: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        pg = browser.new_page()

        # Collect every WS that matches /ws/session/new across the whole flow.
        pg.on(
            "websocket",
            lambda ws: session_new_urls.append(ws.url) if "/ws/session/new" in ws.url else None,
        )

        pg.goto(f"http://localhost:{backend_port}")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_selector("#canvas", timeout=15000)
        pg.wait_for_function("() => window.__claudeRtsBootComplete === true", timeout=15000)

        initial_count = pg.evaluate("() => cards.length")

        # Ensure hubs are loaded before opening the context menu — hubs are
        # fetched asynchronously during boot and may still be an empty array
        # when __claudeRtsBootComplete fires.
        pg.wait_for_function("() => typeof hubs !== 'undefined' && hubs.length > 0", timeout=5000)

        # Open context menu and click the first hub-based spawn item.
        open_context_menu(pg, 500, 500)
        first_spawn_item = pg.locator("#context-menu.visible .ctx-item[data-hub]").first
        first_spawn_item.wait_for(state="visible", timeout=5000)
        first_spawn_item.click()

        # (1) A new card is appended to cards[].
        pg.wait_for_function(
            f"() => cards.length > {initial_count}",
            timeout=10000,
        )

        # (2) Give any spurious /ws/session/new time to manifest before asserting.
        time.sleep(0.5)
        assert not session_new_urls, (
            f"Expected NO /ws/session/new during dormant spawn, got: {session_new_urls}"
        )

        # (3) The new card is dormant: starred === false AND DOM has the
        # dormant placeholder ("Dormant — unstarred" label rendered by
        # TerminalCard._renderDormant).
        last_starred = pg.evaluate("() => cards[cards.length - 1].starred")
        assert last_starred is False, f"Expected new card.starred=false, got {last_starred!r}"

        last_card_id = pg.evaluate("() => cards[cards.length - 1].id")
        dormant_text = pg.evaluate(
            "(cardId) => { "
            "const el = document.querySelector(`[data-card-id=\"${cardId}\"]`); "
            "return el ? el.innerText : null; }",
            last_card_id,
        )
        assert dormant_text is not None, f"Card DOM not found for id={last_card_id}"
        assert "Dormant" in dormant_text, (
            f"Expected dormant placeholder in card DOM, got: {dormant_text!r}"
        )

        pg.close()
        browser.close()


# ── S19: Per-device fields absent from canvas JSON snapshot ───────────────────


def test_per_device_fields_not_in_canvas_snapshot(backend_server, backend_port):
    """S19: pan, zoom, controlGroups do NOT appear in GET /api/canvases/{name} snapshot.

    After panning and zooming in the browser and forcing a state mutation
    (which triggers _persist_canvas_snapshot), the returned canvas JSON must
    not contain per-device fields.
    """
    headed = os.environ.get("HEADED", "").lower() in ("1", "true")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        pg = browser.new_page()

        pg.goto(f"http://localhost:{backend_port}")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_selector("#canvas", timeout=15000)
        pg.wait_for_function("() => window.__claudeRtsBootComplete === true", timeout=15000)

        # Trigger a pan/zoom via JS (per-device state, should not persist).
        pg.evaluate(
            """() => {
            if (typeof pan === 'object' && pan !== null) { pan.x = 500; pan.y = 300; }
            if (typeof zoom !== 'undefined') { zoom = 1.5; }
            if (typeof applyTransform === 'function') applyTransform();
        }"""
        )

        # Force a state mutation to trigger a snapshot persist.
        # We use the REST API directly to set starred=true on the first card.
        first_card_id = pg.evaluate(
            "() => { const c = cards.find(c => c.cardId || c.sessionId); return c ? (c.cardId || c.sessionId) : null; }"
        )

        if first_card_id:
            pg.evaluate(
                f"""async () => {{
                await fetch('/api/cards/{first_card_id}/state', {{
                    method: 'PUT',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{starred: true}}),
                }});
            }}"""
            )

        # Fetch the canvas snapshot via the REST API.
        canvas_name = pg.evaluate(
            """async () => {
            const r = await fetch('/api/config');
            if (!r.ok) return 'stress-layout';
            const cfg = await r.json();
            return cfg.default_canvas || 'stress-layout';
        }"""
        )

        snap = pg.evaluate(
            f"""async () => {{
            const r = await fetch('/api/canvases/{canvas_name}');
            if (!r.ok) return {{}};
            return r.json();
        }}"""
        )

        # Per-device fields must NOT appear at canvas level.
        assert "pan" not in snap, f"'pan' leaked into canvas snapshot: {snap}"
        assert "zoom" not in snap, f"'zoom' leaked into canvas snapshot: {snap}"
        assert "controlGroups" not in snap, f"'controlGroups' leaked into canvas snapshot: {snap}"

        # Per-device fields must NOT appear in card entries either.
        for card_entry in snap.get("cards", []):
            assert "pan" not in card_entry, f"'pan' leaked into card entry: {card_entry}"
            assert "zoom" not in card_entry, f"'zoom' leaked into card entry: {card_entry}"

        pg.close()
        browser.close()
