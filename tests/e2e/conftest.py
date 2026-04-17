"""Playwright fixtures for supreme-claudemander e2e tests.

Launches the Python backend with --dev-config <preset>, then opens the
app in a Chromium browser via Playwright.

The dev-config preset defaults to "stress-test" and can be overridden
per-module by defining a ``dev_config_preset`` fixture.

This module is also the single source of truth for shared helpers used
across e2e test files:

- ``clear_canvas``          — destroy all cards + clear canvas DOM, condition-based
- ``open_context_menu``     — right-click viewport and wait for menu
- ``refresh_vm_card``       — re-render VM Manager widgets, condition-based
- ``cleanup_non_vm_cards``  — remove non-VM cards, condition-based

Test files should import these helpers from conftest rather than
redefining them locally (see issue #165).
"""

import os
import subprocess
import sys
import tempfile
import time

import pytest

# Skip the entire e2e module if playwright is not installed
pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright  # noqa: E402


def _wait_for_server(port: int, timeout: float = 30.0) -> bool:
    """Poll until the backend responds on the given port."""
    import urllib.request
    import urllib.error

    deadline = time.monotonic() + timeout
    url = f"http://localhost:{port}/api/config"
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


@pytest.fixture(scope="module")
def dev_config_preset():
    """Override in test modules to use a different dev-config preset."""
    return "stress-test"


@pytest.fixture(scope="module")
def backend_port(dev_config_preset):
    """Return the port for the backend server.

    Each preset gets a deterministic port to avoid collisions when
    pytest-xdist or manual runs overlap.
    """
    base = int(os.environ.get("CLAUDE_RTS_PORT", "3099"))
    # Simple hash so different presets get different ports
    offset = sum(ord(c) for c in dev_config_preset) % 100
    return base + offset


@pytest.fixture(scope="module")
def backend_server(backend_port, dev_config_preset):
    """Start the backend server for the test module."""
    env = os.environ.copy()
    env["CLAUDE_RTS_TEST_MODE"] = "1"

    # Write server output to temp files instead of PIPE to avoid blocking
    # when the pipe buffer fills (the server emits a lot of debug output).
    stdout_file = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, prefix="rts-stdout-")
    stderr_file = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, prefix="rts-stderr-")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "claude_rts",
            "--port",
            str(backend_port),
            "--no-browser",
            "--dev-config",
            dev_config_preset,
            "--test-mode",
        ],
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
    )

    if not _wait_for_server(backend_port, timeout=30.0):
        proc.terminate()
        stdout_file.close()
        stderr_file.close()
        stdout = open(stdout_file.name, errors="replace").read()
        stderr = open(stderr_file.name, errors="replace").read()
        os.unlink(stdout_file.name)
        os.unlink(stderr_file.name)
        pytest.fail(f"Backend did not start within timeout.\nstdout: {stdout}\nstderr: {stderr}")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    for f in (stdout_file, stderr_file):
        try:
            f.close()
            os.unlink(f.name)
        except OSError:
            pass  # Windows may hold the file briefly after process exit


@pytest.fixture(scope="module")
def page(backend_server, backend_port):
    """Launch Chromium and navigate to the backend URL."""
    headed = os.environ.get("HEADED", "").lower() in ("1", "true")

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=not headed)
    pg = browser.new_page()
    pg.goto(f"http://localhost:{backend_port}")
    pg.wait_for_load_state("networkidle")
    pg.wait_for_selector("#canvas", timeout=15000)
    # Wait for the observable boot-complete signal set at the end of the
    # boot IIFE in static/index.html.  Replaces a fixed 3-second sleep so
    # the suite doesn't pay that cost on every module and is insulated from
    # fast/slow-start variance.
    pg.wait_for_function(
        "() => window.__claudeRtsBootComplete === true",
        timeout=15000,
    )

    yield pg

    pg.close()
    browser.close()
    pw.stop()


# ── Shared helpers (single source of truth — see issue #165) ─────────────────


def clear_canvas(page):
    """Destroy all cards, clear the canvas DOM, and reset shared state.

    Condition-based: waits for ``cards.length === 0`` AND
    ``#canvas.children.length === 0`` before returning.  No fixed sleeps.
    The control-WebSocket ``onmessage`` handler is nulled while draining so
    a late ``card_created`` broadcast cannot race the cleanup; it is
    re-attached once the canvas is confirmed empty.
    """
    page.evaluate(
        """() => {
        // Silence queued control-ws messages while we drain cards.  Cards
        // destroyed below may not yet have a sessionId if the server was
        // slow to respond; in that case _markSessionDestroyed(null) is a
        // no-op and a delayed card_created broadcast would otherwise slip
        // through the guard.  Keeping the handler null until after
        // wait_for_function prevents ghost cards from appearing while we
        // wait for the canvas to drain.
        if (typeof controlWs !== 'undefined' && controlWs) {
            try { controlWs.onmessage = null; } catch(e) {}
        }
        if (typeof cards !== 'undefined') {
            for (const card of cards) {
                if (typeof card.destroy === 'function') card.destroy();
            }
            cards.length = 0;
        }
        if (typeof controlGroups !== 'undefined') {
            controlGroups.clear
                ? controlGroups.clear()
                : Object.keys(controlGroups).forEach(k => delete controlGroups[k]);
        }
        const el = document.getElementById('canvas');
        if (el) el.innerHTML = '';
        // Reset context menu and pan/zoom so residual DOM state cannot
        // block the right-click handler on subsequent tests.
        if (typeof hideContextMenu === 'function') hideContextMenu();
        if (typeof pan === 'object' && pan !== null) { pan.x = 0; pan.y = 0; }
        if (typeof zoom !== 'undefined') { zoom = 1; }
        if (typeof applyTransform === 'function') applyTransform();
        if (typeof focusedCardId !== 'undefined') { focusedCardId = null; }
    }"""
    )
    # Wait until the canvas DOM is truly empty.  Re-clear on every poll to
    # evict ghost cards that snuck in while a card_created handler was
    # already mid-execution when we nulled controlWs.onmessage above.
    page.wait_for_function(
        """() => {
            if (typeof controlWs !== 'undefined' && controlWs) {
                try { controlWs.onmessage = null; } catch(e) {}
            }
            if (typeof cards !== 'undefined' && cards.length > 0) {
                for (const card of cards) {
                    if (typeof card.destroy === 'function') card.destroy();
                }
                cards.length = 0;
            }
            const el = document.getElementById('canvas');
            if (el && el.children.length > 0) el.innerHTML = '';
            return el !== null && el.children.length === 0 &&
                   ((typeof cards !== 'undefined') ? cards.length : 0) === 0;
        }""",
        timeout=3000,
    )
    # Re-attach the control-ws message handler now that the canvas is
    # confirmed empty.  Any broadcast that arrives from this point on goes
    # through the normal handleControlCardCreated guard logic.
    page.evaluate(
        """() => {
        if (typeof controlWs !== 'undefined' && controlWs) {
            try {
                controlWs.onmessage = (ev) => {
                    try {
                        const msg = JSON.parse(ev.data);
                        if (msg.type === 'card_created') handleControlCardCreated(msg);
                        else if (msg.type === 'card_deleted') handleControlCardDeleted(msg);
                        else if (msg.type === 'card_updated') handleControlCardUpdated(msg);
                        else if (msg.type === 'blueprint:log') handleBlueprintLog(msg);
                        else if (msg.type === 'blueprint:completed') handleBlueprintCompleted(msg);
                        else if (msg.type === 'blueprint:failed') handleBlueprintFailed(msg);
                        else if (msg.type === 'blueprint:open_widget') handleBlueprintOpenWidget(msg);
                    } catch(e) {}
                };
            } catch(e) {}
        }
    }"""
    )


def open_context_menu(page, x=500, y=500):
    """Right-click the viewport at ``(x, y)`` and wait for #context-menu visible with items."""
    # Dismiss any leftover menu from a prior test before right-clicking,
    # otherwise the visible menu intercepts pointer events.
    page.evaluate("() => { if (typeof hideContextMenu === 'function') hideContextMenu(); }")
    page.locator("#viewport").click(button="right", position={"x": x, "y": y})
    # Wait for menu to be visible AND contain at least one item.
    # If the right-click was intercepted by a ghost card, showContextMenu never
    # fires and this times out with a clear error.
    page.locator(
        "#context-menu.visible .ctx-item[data-hub], #context-menu.visible .ctx-item[data-host-shell]"
    ).first.wait_for(state="visible", timeout=5000)


def refresh_vm_card(page):
    """Force re-render of all VM Manager widget cards; wait until the DOM
    reflects the current ``/api/vms/favorites`` response.

    The VM Manager ``render()`` method is async (fetches favorites +
    discover + config before writing ``body.innerHTML``).  We read the
    expected favorite names from the API and poll until every favorite has
    a ``[data-vm-remove="<name>"]`` button in the DOM (or favorites is
    empty and ``[data-vm-search]`` is present).  This replaces fixed
    ``wait_for_timeout`` delays that were 1.5 s in mock tests and 3 s in
    real-Docker tests — both readily slower than the slowest observed
    render and prone to flakes on busy CI.
    """
    page.evaluate(
        """() => {
        if (typeof cards !== 'undefined') {
            for (const card of cards) {
                if (card.widgetType === 'vm-manager' && typeof card.render === 'function') {
                    card.render();
                }
            }
        }
    }"""
    )
    # Fetch expected favorite names, then wait for them to appear.  Using
    # data-vm-remove (one per favorite, regardless of state) is the most
    # reliable observable for render completion.
    page.wait_for_function(
        """async () => {
            const r = await fetch('/api/vms/favorites');
            if (!r.ok) return false;
            const favs = await r.json();
            // The VM Manager card must be present in the DOM.
            const searchInputs = document.querySelectorAll('[data-vm-search]');
            if (searchInputs.length === 0) return false;
            if (favs.length === 0) {
                // Empty favorites: confirm no remove buttons exist (clean render) and search input present
                const removeBtns = document.querySelectorAll('[data-vm-remove]');
                return removeBtns.length === 0 && searchInputs.length > 0;
            }
            for (const fav of favs) {
                const btn = document.querySelector(
                    `[data-vm-remove="${CSS.escape(fav.name)}"]`
                );
                if (!btn) return false;
            }
            return true;
        }""",
        timeout=10000,
    )


def cleanup_non_vm_cards(page):
    """Remove all cards except VM Manager widgets.

    Terminal cards spawned by earlier tests can intercept pointer events
    on the VM Manager card.  This helper destroys them and waits until
    every remaining ``[data-card-id]`` contains a ``[data-vm-search]``
    descendant (i.e. only VM Manager cards remain) — no fixed sleep.
    """
    page.evaluate(
        """() => {
        if (typeof cards === 'undefined') return;
        const toRemove = [];
        for (let i = cards.length - 1; i >= 0; i--) {
            if (cards[i].widgetType !== 'vm-manager') {
                if (typeof cards[i].destroy === 'function') cards[i].destroy();
                toRemove.push(i);
            }
        }
        for (const idx of toRemove) cards.splice(idx, 1);
    }"""
    )
    page.wait_for_function(
        """() => {
            const all = document.querySelectorAll('[data-card-id]');
            return Array.from(all).every(
                c => c.querySelector('[data-vm-search]') !== null
            );
        }""",
        timeout=3000,
    )
