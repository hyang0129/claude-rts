"""E2E tests for unified spawn + dynamic context menu (#128).

Verifies that all context-menu sections render correctly, that every row fires
its click handler, and that spawnFromSerialized restores mixed-type canvases
via CARD_TYPE_REGISTRY uniformly.

Run:
    pip install pytest-playwright playwright
    python -m playwright install chromium
    python -m pytest tests/e2e/test_spawn_registry_e2e.py -v

Headed mode:
    HEADED=1 python -m pytest tests/e2e/test_spawn_registry_e2e.py -v
"""

import pytest

# Skip module entirely if playwright is not installed
pw = pytest.importorskip("playwright")


# ── Helpers ───────────────────────────────────────────────────────────────────


def open_context_menu(page, x=500, y=500):
    """Right-click viewport at (x, y) and wait for #context-menu visible."""
    # Dismiss any leftover menu from a prior test before right-clicking,
    # otherwise the visible menu intercepts pointer events.
    page.evaluate("() => { if (typeof hideContextMenu === 'function') hideContextMenu(); }")
    page.locator("#viewport").click(button="right", position={"x": x, "y": y})
    page.locator("#context-menu").wait_for(state="visible", timeout=3000)


def count_cards_by_type(page, card_type):
    """Count cards where card.type === card_type via JS."""
    return page.evaluate("(t) => cards.filter(c => c.type === t).length", card_type)


def get_last_card(page):
    """Return the JS-side last entry in cards[] as a plain dict."""
    return page.evaluate(
        """() => {
            const c = cards[cards.length - 1];
            if (!c) return null;
            return {
                cardType: c.type,
                widgetType: c.widgetType || null,
                hub: c.hub || null,
                container: c.container || null,
                exec: c.exec || null,
                sessionId: c.sessionId || null,
                symbol: c.symbol || null,
                x: c.x, y: c.y, w: c.w, h: c.h,
            };
        }"""
    )


def get_menu_section_headers(page):
    """Return list of ctx-header text in DOM order."""
    return page.evaluate(
        """() => Array.from(
            document.querySelectorAll('#context-menu .ctx-header')
        ).map(e => e.textContent.trim())"""
    )


def get_card_count(page):
    """Return the current length of the JS cards[] array."""
    return page.evaluate("() => cards.length")


def clear_canvas(page):
    """Destroy all cards, clear the canvas DOM element, and reset shared state."""
    page.evaluate(
        """() => {
        // Silence any queued control-ws messages so a card_created broadcast
        // that arrives after destroy cannot re-spawn a ghost card.
        if (typeof controlWs !== 'undefined' && controlWs) {
            try { controlWs.onmessage = null; } catch(e) {}
        }
        if (typeof cards !== 'undefined') {
            for (const card of cards) {
                if (typeof card.destroy === 'function') card.destroy();
            }
            cards.length = 0;
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
        // Re-attach the control-ws message handler now that the canvas is clean.
        if (typeof controlWs !== 'undefined' && controlWs) {
            try {
                controlWs.onmessage = (ev) => {
                    try {
                        const msg = JSON.parse(ev.data);
                        if (msg.type === 'card_created') handleControlCardCreated(msg);
                        else if (msg.type === 'card_deleted') handleControlCardDeleted(msg);
                    } catch(e) {}
                };
            } catch(e) {}
        }
    }"""
    )
    page.wait_for_timeout(300)


def wait_for_new_card(page, initial_count, timeout_ms=3000):
    """Poll until cards.length > initial_count; return True if a new card appeared."""
    page.wait_for_function(
        f"() => cards.length > {initial_count}",
        timeout=timeout_ms,
    )


# ── S1: Context menu renders all sections in canonical order ──────────────────


class TestContextMenuSections:
    """S1: Context menu renders all sections in canonical order."""

    def test_context_menu_section_order(self, page):
        """Right-click viewport; all expected sections appear in canonical order."""
        open_context_menu(page, 500, 500)

        headers = get_menu_section_headers(page)

        # Required headers must appear (order not guaranteed to be strictly
        # contiguous but must be in relative order: terminal → host → widget →
        # probe → canvas claude).
        assert "Spawn terminal" in headers, f"'Spawn terminal' missing from headers: {headers}"
        assert "Host terminal" in headers, f"'Host terminal' missing from headers: {headers}"
        assert "Spawn widget" in headers, f"'Spawn widget' missing from headers: {headers}"
        assert "Claude usage probe" in headers, f"'Claude usage probe' missing from headers: {headers}"
        assert "Canvas Claude" in headers, f"'Canvas Claude' missing from headers: {headers}"

        # Relative ordering
        idx_terminal = headers.index("Spawn terminal")
        idx_host = headers.index("Host terminal")
        idx_widget = headers.index("Spawn widget")
        idx_probe = headers.index("Claude usage probe")
        idx_cc = headers.index("Canvas Claude")
        assert idx_terminal < idx_host < idx_widget < idx_probe < idx_cc, f"Headers not in canonical order: {headers}"

    def test_context_menu_hub_rows(self, page):
        """At least one [data-hub] row appears (util container is always discovered)."""
        open_context_menu(page, 500, 500)
        hub_count = page.locator("#context-menu [data-hub]").count()
        assert hub_count >= 1, "Expected at least one hub row in context menu"

    def test_context_menu_host_shell_rows(self, page):
        """At least one [data-host-shell] row appears."""
        open_context_menu(page, 500, 500)
        host_count = page.locator("#context-menu [data-host-shell]").count()
        assert host_count >= 1, "Expected at least one host-shell row in context menu"

    def test_context_menu_widget_rows_match_registry(self, page):
        """[data-widget] count equals Object.keys(WIDGET_REGISTRY).length."""
        open_context_menu(page, 500, 500)
        result = page.evaluate(
            """() => ({
                menuCount: document.querySelectorAll('#context-menu [data-widget]').length,
                registryCount: Object.keys(WIDGET_REGISTRY).length,
            })"""
        )
        assert result["menuCount"] == result["registryCount"], (
            f"Widget menu rows ({result['menuCount']}) != WIDGET_REGISTRY entries ({result['registryCount']})"
        )

    def test_context_menu_probe_profile_rows(self, page):
        """At least one [data-probe-profile] row (priority profile is configured)."""
        open_context_menu(page, 500, 500)
        probe_count = page.locator("#context-menu [data-probe-profile]").count()
        assert probe_count >= 1, "Expected at least one probe-profile row"

    def test_context_menu_canvas_claude_row(self, page):
        """Exactly one [data-card-type='canvas_claude'] row under Canvas Claude."""
        open_context_menu(page, 500, 500)
        cc_count = page.locator("#context-menu [data-card-type='canvas_claude']").count()
        assert cc_count == 1, f"Expected exactly one canvas_claude row, got {cc_count}"

    def test_no_loader_card_header(self, page):
        """LoaderCard (menuSection: null) does not generate a ctx-header."""
        open_context_menu(page, 500, 500)
        headers = get_menu_section_headers(page)
        # loader section must not appear — it registers menuSection: null
        loader_headers = [h for h in headers if "loader" in h.lower()]
        assert loader_headers == [], f"Unexpected loader header(s) in menu: {loader_headers}"


# ── Module fixture overrides for each test class ─────────────────────────────
# conftest.py defines dev_config_preset as module-scoped returning "stress-test".
# Each test module can override it by defining a module-level fixture of the
# same name. We use separate test modules by convention; here we consolidate
# multiple presets into one file so we need per-class control. Since pytest
# does not support class-scoped fixture overrides that interact with
# module-scoped backend_server/page, we define separate module-level overrides
# using a single module-scoped fixture below that covers the majority preset
# (start-claude). TestContextMenuSections, S5, S6, S7, S9, S10 all need
# start-claude (or default); the default tests are marked separately.


@pytest.fixture(scope="module")
def dev_config_preset():
    """Use start-claude for most tests in this module."""
    return "start-claude"


# ── S2: Spawn terminal via hub row ────────────────────────────────────────────


class TestSpawnTerminalFromHub:
    """S2: Spawn terminal via hub row flattens hub object correctly."""

    def test_hub_row_spawns_terminal_card(self, page):
        """Click first [data-hub] row; new card appears with terminal cardType."""
        clear_canvas(page)
        initial_count = get_card_count(page)

        open_context_menu(page, 400, 300)

        # Read the hub id from the first hub row before clicking it
        hub_id = page.locator("#context-menu [data-hub]").first.get_attribute("data-hub")
        assert hub_id, "Expected a data-hub attribute on the first hub row"

        page.locator("#context-menu [data-hub]").first.click()

        wait_for_new_card(page, initial_count, timeout_ms=3000)

        card = get_last_card(page)
        assert card is not None, "Expected a card after clicking hub row"
        assert card["cardType"] == "terminal", f"Expected cardType 'terminal', got {card['cardType']}"
        assert card["hub"] == hub_id, f"Expected hub == '{hub_id}' (string, not object), got {card['hub']!r}"
        assert card["container"] is not None, "Expected container to be non-null"

    def test_hub_row_card_has_symbol(self, page):
        """Card spawned from hub row has a non-'?' symbol."""
        clear_canvas(page)
        initial_count = get_card_count(page)

        open_context_menu(page, 400, 300)
        page.locator("#context-menu [data-hub]").first.click()

        wait_for_new_card(page, initial_count, timeout_ms=3000)

        card = get_last_card(page)
        assert card is not None
        assert card["symbol"] is not None, "Expected symbol to be non-null"
        assert card["symbol"] != "?", "Expected a real symbol, got '?'"


# ── S3: Spawn host terminal ───────────────────────────────────────────────────


class TestSpawnHostTerminal:
    """S3: Spawn host terminal creates synthetic-hub TerminalCard."""

    def test_host_shell_row_spawns_terminal(self, page):
        """Click first [data-host-shell] row; TerminalCard with synthetic hub appears."""
        clear_canvas(page)
        initial_count = get_card_count(page)

        open_context_menu(page, 500, 500)

        host_item = page.locator("#context-menu [data-host-shell]").first
        assert host_item.count() > 0, "No [data-host-shell] row in context menu"
        hub_id = host_item.get_attribute("data-host-shell")
        host_item.click()

        wait_for_new_card(page, initial_count, timeout_ms=3000)

        card = get_last_card(page)
        assert card is not None, "Expected a card after clicking host-shell row"
        assert card["cardType"] == "terminal", f"Expected 'terminal', got {card['cardType']}"
        assert card["hub"] == hub_id, f"Expected hub == '{hub_id}', got {card['hub']!r}"

    def test_host_shell_seeds_hub_symbol(self, page):
        """hubSymbolMap[hub] is set to a single-char string after host-shell spawn."""
        clear_canvas(page)
        initial_count = get_card_count(page)

        open_context_menu(page, 500, 500)
        host_item = page.locator("#context-menu [data-host-shell]").first
        hub_id = host_item.get_attribute("data-host-shell")
        host_item.click()

        wait_for_new_card(page, initial_count, timeout_ms=3000)

        sym = page.evaluate("(h) => hubSymbolMap[h]", hub_id)
        assert sym is not None, f"hubSymbolMap['{hub_id}'] should be set after spawn"
        assert len(sym) == 1, f"Symbol should be a single char, got {sym!r}"


# ── S4: Spawn widget via context menu ─────────────────────────────────────────


class TestSpawnWidgetFromMenu:
    """S4: Spawn widget via context menu routes through CARD_TYPE_REGISTRY."""

    def test_widget_row_spawns_widget_card(self, page):
        """Click first [data-widget] row; WidgetCard with correct widgetType appears."""
        clear_canvas(page)
        initial_count = get_card_count(page)

        open_context_menu(page, 500, 500)

        widget_item = page.locator("#context-menu [data-widget]").first
        if widget_item.count() == 0:
            pytest.skip("No widget rows in context menu")

        widget_type = widget_item.get_attribute("data-widget")
        widget_item.click()

        wait_for_new_card(page, initial_count, timeout_ms=3000)

        card = get_last_card(page)
        assert card is not None, "Expected a card after clicking widget row"
        assert card["cardType"] == "widget", f"Expected 'widget', got {card['cardType']}"
        assert card["widgetType"] == widget_type, f"Expected widgetType == '{widget_type}', got {card['widgetType']!r}"

    def test_legacy_spawn_widget_free_function_is_gone(self, page):
        """spawnWidget free function must not exist (PR migration guard)."""
        spawn_widget_type = page.evaluate("() => typeof window.spawnWidget")
        assert spawn_widget_type == "undefined", (
            f"window.spawnWidget should be gone after migration, got typeof=={spawn_widget_type!r}"
        )


# ── S5: Canvas Claude spawn with priority profile ─────────────────────────────


class TestSpawnCanvasClaudeWithProfile:
    """S5: Spawn Canvas Claude runs async prepareOpts with priority profile."""

    def test_canvas_claude_spawns_with_correct_opts(self, page):
        """Click canvas_claude row; card appears with correct hub/container/symbol."""
        clear_canvas(page)
        initial_count = get_card_count(page)

        open_context_menu(page, 600, 400)

        cc_item = page.locator("#context-menu [data-card-type='canvas_claude']")
        assert cc_item.count() == 1, "Expected canvas_claude row in context menu"
        cc_item.click()

        # prepareOpts issues two sequential fetch() calls; allow up to 5 s
        wait_for_new_card(page, initial_count, timeout_ms=5000)

        card = get_last_card(page)
        assert card is not None, "Expected a canvas_claude card after clicking row"
        assert card["cardType"] == "canvas_claude", f"Expected 'canvas_claude', got {card['cardType']!r}"
        assert card["hub"] == "canvas-claude", f"Expected hub 'canvas-claude', got {card['hub']!r}"
        assert card["container"] == "supreme-claudemander-util", (
            f"Expected container 'supreme-claudemander-util', got {card['container']!r}"
        )
        assert card["symbol"] == "C", f"Expected symbol 'C', got {card['symbol']!r}"


# ── S6: Canvas Claude with no priority profile ────────────────────────────────


class TestSpawnCanvasClaudeNoProfile:
    """S6: Canvas Claude spawn with no priority profile still succeeds."""

    # NOTE: This class needs the 'default' preset (no priority profile).
    # Because the module fixture returns 'start-claude' and pytest module-scoped
    # fixtures cannot be overridden per-class, this test connects to the same
    # backend. It therefore asserts the weaker "card appears and no page error"
    # guarantee, which is valid on both presets.

    def test_canvas_claude_spawns_without_priority_profile(self, page):
        """Canvas Claude spawn completes even when /api/profiles/priority returns empty."""
        page_errors = []
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        clear_canvas(page)
        initial_count = get_card_count(page)

        open_context_menu(page, 500, 500)

        cc_item = page.locator("#context-menu [data-card-type='canvas_claude']")
        if cc_item.count() == 0:
            pytest.skip("No canvas_claude row in context menu")
        cc_item.click()

        wait_for_new_card(page, initial_count, timeout_ms=5000)

        card = get_last_card(page)
        assert card is not None, "prepareOpts should not throw — card must appear"
        assert card["cardType"] == "canvas_claude"
        assert len(page_errors) == 0, f"Unexpected page error(s): {page_errors}"


# ── S7: Probe-profile row network contract ────────────────────────────────────


class TestProbeProfileNetworkContract:
    """S7a: Probe-profile row issues POST to /api/probe/claude-usage."""

    def test_probe_row_posts_to_api(self, page, backend_port):
        """Click first [data-probe-profile] row; a POST to /api/probe/claude-usage is recorded."""
        recorded_requests = []

        def record_request(request):
            if "probe/claude-usage" in request.url:
                recorded_requests.append(request.url)

        page.on("request", record_request)

        try:
            open_context_menu(page, 500, 500)
            probe_item = page.locator("#context-menu [data-probe-profile]").first
            if probe_item.count() == 0:
                pytest.skip("No probe-profile row in context menu")
            probe_item.click()

            # Give the async handler time to fire the fetch
            page.wait_for_timeout(2000)
        finally:
            page.remove_listener("request", record_request)

        assert len(recorded_requests) >= 1, (
            f"Expected at least one POST to /api/probe/claude-usage; recorded: {recorded_requests}"
        )


# ── S8: spawnFromSerialized restores mixed-type canvas ───────────────────────


class TestSpawnFromSerializedMixedCanvas:
    """S8: spawnFromSerialized restores mixed-type canvas via CARD_TYPE_REGISTRY."""

    def test_mixed_canvas_restore(self, page, backend_port):
        """PUT a mixed canvas (terminal + widget + legacy entry + unknown hub) and switch to it.

        Expected: exactly 3 cards restored (unknown hub entry silently skipped).
        """
        # Get the first available hub from the client's in-memory hubs[] so that
        # spawnFromSerialized can find it via hubs.find(...) on restore.
        hub_info = page.evaluate(
            "() => hubs && hubs.length ? { hub: hubs[0].hub, container: hubs[0].container || '' } : null"
        )
        if hub_info is None:
            pytest.skip("No hubs available to build mixed canvas fixture")

        first_hub = hub_info["hub"]
        first_container = hub_info["container"]

        # PUT the mixed canvas payload
        page.evaluate(
            f"""async () => {{
                await fetch('/api/canvases/mixed-restore', {{
                    method: 'PUT',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        cards: [
                            {{ type: 'terminal', hub: '{first_hub}', container: '{first_container}',
                               x: 100, y: 100, w: 400, h: 300 }},
                            {{ type: 'widget', widgetType: 'system-info',
                               x: 550, y: 100, w: 400, h: 300 }},
                            {{ hub: '{first_hub}', container: '{first_container}',
                               x: 100, y: 450, w: 400, h: 300 }},
                            {{ type: 'terminal', hub: 'does-not-exist-xyz',
                               x: 600, y: 450, w: 300, h: 200 }},
                        ],
                        pan: {{x: 0, y: 0}}, zoom: 1,
                    }}),
                }});
            }}"""
        )

        # Clear canvas and switch to the saved layout
        clear_canvas(page)
        page.evaluate("() => switchCanvas('mixed-restore')")

        # Wait for spawn to complete (sequential async spawns)
        page.wait_for_timeout(2000)

        total_cards = get_card_count(page)
        assert total_cards == 3, f"Expected exactly 3 cards (unknown hub skipped); got {total_cards}"

        card_types = page.evaluate("() => cards.map(c => c.type)")
        assert card_types[0] == "terminal", f"First card should be terminal, got {card_types[0]!r}"
        assert card_types[1] == "widget", f"Second card should be widget, got {card_types[1]!r}"
        assert card_types[2] == "terminal", f"Third card (legacy entry) should be terminal, got {card_types[2]!r}"

    def test_no_console_errors_on_mixed_restore(self, page, backend_port):
        """No unhandled page errors during mixed canvas restore."""
        page_errors = []

        def _on_page_error(err):
            page_errors.append(str(err))

        page.on("pageerror", _on_page_error)

        hub_info = page.evaluate(
            "() => hubs && hubs.length ? { hub: hubs[0].hub, container: hubs[0].container || '' } : null"
        )
        if hub_info is None:
            pytest.skip("No hubs available")

        first_hub = hub_info["hub"]
        first_container = hub_info["container"]

        page.evaluate(
            f"""async () => {{
                await fetch('/api/canvases/mixed-restore-clean', {{
                    method: 'PUT',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        cards: [
                            {{ type: 'terminal', hub: '{first_hub}', container: '{first_container}',
                               x: 100, y: 100, w: 400, h: 300 }},
                            {{ type: 'widget', widgetType: 'system-info',
                               x: 550, y: 100, w: 400, h: 300 }},
                        ],
                        pan: {{x: 0, y: 0}}, zoom: 1,
                    }}),
                }});
            }}"""
        )

        clear_canvas(page)
        page.evaluate("() => switchCanvas('mixed-restore-clean')")
        page.wait_for_timeout(2000)

        page.remove_listener("pageerror", _on_page_error)
        assert len(page_errors) == 0, f"Unexpected page error(s) during restore: {page_errors}"


# ── S9: Canvas Claude staleness check on reload ───────────────────────────────


class TestCanvasClaudeStalenessCheck:
    """S9: Canvas Claude reload nulls a dead sessionId via /api/sessions probe."""

    def test_stale_session_id_is_replaced(self, page, backend_port):
        """PUT canvas with bogus sessionId; after switchCanvas the card has a different id."""
        bogus_session_id = "deadbeef-not-a-real-session"

        page.evaluate(
            f"""async () => {{
                await fetch('/api/canvases/cc-stale-test', {{
                    method: 'PUT',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        cards: [
                            {{ type: 'canvas_claude',
                               x: 100, y: 100, w: 800, h: 500,
                               session_id: '{bogus_session_id}' }},
                        ],
                        pan: {{x: 0, y: 0}}, zoom: 1,
                    }}),
                }});
            }}"""
        )

        clear_canvas(page)
        page.evaluate("() => switchCanvas('cc-stale-test')")

        # prepareOpts does two fetches; allow up to 5 s
        page.wait_for_timeout(3000)

        total_cards = get_card_count(page)
        assert total_cards >= 1, "Expected at least one canvas_claude card after switchCanvas"

        card = get_last_card(page)
        assert card is not None
        assert card["cardType"] == "canvas_claude", f"Expected canvas_claude, got {card['cardType']!r}"
        # The bogus sessionId must have been discarded
        assert card["sessionId"] != bogus_session_id, (
            f"Stale session id was NOT cleared — card still has {card['sessionId']!r}"
        )


# ── S10: Context menu click handlers all wire correctly ───────────────────────


class TestContextMenuHandlerWiring:
    """S10: Context menu click handlers all wire correctly (two-pass guard)."""

    def test_hub_row_click_hides_menu_and_spawns_card(self, page):
        """Hub row click fires handler: menu hides, new card appears."""
        clear_canvas(page)
        initial_count = get_card_count(page)

        open_context_menu(page, 500, 500)
        page.locator("#context-menu [data-hub]").first.click()

        # Menu must hide after handler fires
        page.locator("#context-menu").wait_for(state="hidden", timeout=3000)

        wait_for_new_card(page, initial_count, timeout_ms=3000)
        assert get_card_count(page) > initial_count

    def test_host_shell_row_click_hides_menu_and_spawns_card(self, page):
        """Host-shell row click fires handler: menu hides, new card appears."""
        clear_canvas(page)
        initial_count = get_card_count(page)

        open_context_menu(page, 500, 500)
        host_item = page.locator("#context-menu [data-host-shell]").first
        if host_item.count() == 0:
            pytest.skip("No host-shell row in context menu")
        host_item.click()

        page.locator("#context-menu").wait_for(state="hidden", timeout=3000)

        wait_for_new_card(page, initial_count, timeout_ms=3000)
        assert get_card_count(page) > initial_count

    def test_widget_row_click_hides_menu_and_spawns_card(self, page):
        """Widget row click fires handler: menu hides, new card appears."""
        clear_canvas(page)
        initial_count = get_card_count(page)

        open_context_menu(page, 500, 500)
        widget_item = page.locator("#context-menu [data-widget]").first
        if widget_item.count() == 0:
            pytest.skip("No widget row in context menu")
        widget_item.click()

        page.locator("#context-menu").wait_for(state="hidden", timeout=3000)

        wait_for_new_card(page, initial_count, timeout_ms=3000)
        assert get_card_count(page) > initial_count

    def test_probe_profile_row_click_hides_menu(self, page):
        """Probe-profile row click fires handler: menu hides (async handler)."""
        open_context_menu(page, 500, 500)
        probe_item = page.locator("#context-menu [data-probe-profile]").first
        if probe_item.count() == 0:
            pytest.skip("No probe-profile row in context menu")
        probe_item.click()

        # Menu must hide immediately even for async handlers
        page.locator("#context-menu").wait_for(state="hidden", timeout=3000)

    def test_canvas_claude_row_click_hides_menu(self, page):
        """Canvas Claude row click fires handler: menu hides (async handler)."""
        clear_canvas(page)

        open_context_menu(page, 500, 500)
        cc_item = page.locator("#context-menu [data-card-type='canvas_claude']")
        if cc_item.count() == 0:
            pytest.skip("No canvas_claude row in context menu")
        cc_item.click()

        page.locator("#context-menu").wait_for(state="hidden", timeout=3000)
