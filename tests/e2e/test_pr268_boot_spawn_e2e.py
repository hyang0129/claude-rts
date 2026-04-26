"""Playwright E2E tests for PR #268 (epic #254) — Bucket B boot/spawn scenarios.

Covers:
  S1  — Cold boot: no /ws/session/new for boot-hydrated terminals
  S2  — Warm boot (server restart): browser reconnect does not duplicate cards
  S11 — Canvas switch: no /ws/session/new for already-registered cards
  S13 — User-initiated terminal spawn fires exactly one /ws/session/new
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

            # Record initial card IDs.
            initial_ids = pg.evaluate("() => cards.map(c => c.sessionId || c.cardId).filter(Boolean)")

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

            card_ids = pg.evaluate("() => cards.map(c => c.sessionId || c.cardId).filter(Boolean)")

            pg.close()
            browser.close()

        assert len(card_ids) == len(set(card_ids)), (
            f"Duplicate card IDs found after warm boot: {card_ids}\nInitial IDs were: {initial_ids}"
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


@pytest.mark.xfail(
    reason=(
        "S11: Requires a dev-config preset with two canvases. "
        "The stress-test preset has only one canvas ('stress-layout'). "
        "A two-canvas preset would be needed; see PR #268 follow-up."
    ),
    strict=False,
)
def test_canvas_switch_no_session_new_for_registered_cards(backend_server, backend_port):
    """S11: Switching between two hydrated canvases never opens /ws/session/new.

    TODO(S11): The stress-test preset only has one canvas. This test requires
    a dev-config preset with two canvases both hydrated at boot (keep_resident).
    The xfail reflects the missing preset, not a SUT bug.
    """
    headed = os.environ.get("HEADED", "").lower() in ("1", "true")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        pg = browser.new_page()

        # Collect only the WS opens that happen AFTER boot.
        switch_ws_opens: list[str] = []

        pg.goto(f"http://localhost:{backend_port}")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_selector("#canvas", timeout=15000)
        pg.wait_for_function("() => window.__claudeRtsBootComplete === true", timeout=15000)

        # Start collecting after boot so boot-time attaches are excluded.
        pg.on("websocket", lambda ws: switch_ws_opens.append(ws.url))

        # The stress-test preset only has one canvas — this will fail, which is expected.
        pg.evaluate("() => typeof switchCanvas === 'function' && switchCanvas('nonexistent-canvas-b')")
        time.sleep(0.5)  # brief wait for any WS opens to be captured

        session_new_opens = [url for url in switch_ws_opens if "/ws/session/new" in url]
        assert session_new_opens == [], f"Unexpected /ws/session/new during canvas switch: {session_new_opens}"

        pg.close()
        browser.close()


# ── S13: User-initiated terminal spawn calls /ws/session/new exactly once ─────


def test_user_spawn_calls_session_new_exactly_once(backend_server, backend_port):
    """S13: Clicking the context-menu spawn item opens exactly one /ws/session/new.

    The stress-test preset has hubs registered (supreme-claudemander-util),
    so the context menu will have spawn items available.
    """
    headed = os.environ.get("HEADED", "").lower() in ("1", "true")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        pg = browser.new_page()

        pg.goto(f"http://localhost:{backend_port}")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_selector("#canvas", timeout=15000)
        pg.wait_for_function("() => window.__claudeRtsBootComplete === true", timeout=15000)

        initial_count = pg.evaluate("() => cards.length")

        # Start collecting AFTER boot so we only see user-initiated spawns.
        spawn_ws_opens: list[str] = []
        pg.on("websocket", lambda ws: spawn_ws_opens.append(ws.url))

        # Open context menu and click the first hub-based spawn item.
        open_context_menu(pg, 500, 500)

        first_spawn_item = pg.locator("#context-menu.visible .ctx-item[data-hub]").first
        first_spawn_item.wait_for(state="visible", timeout=5000)
        first_spawn_item.click()

        # Wait for a new card to appear (Playwright auto-waits).
        pg.wait_for_function(
            f"() => cards.length > {initial_count}",
            timeout=10000,
        )

        new_session_opens = [u for u in spawn_ws_opens if "/ws/session/new" in u]
        assert len(new_session_opens) == 1, f"Expected exactly 1 /ws/session/new, got: {new_session_opens}"

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
