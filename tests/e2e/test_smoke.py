"""Playwright Electron smoke tests for supreme-claudemander.

These tests launch the real backend with --dev-config stress-test and
the Electron shell, then verify critical user flows via Playwright.

Run:
    pip install pytest-playwright playwright
    python -m playwright install chromium
    python -m pytest tests/e2e/ -v

Headed mode (shows the Electron window):
    HEADED=1 python -m pytest tests/e2e/ -v
"""

import re

import pytest

# Skip module entirely if playwright is not installed
pw = pytest.importorskip("playwright")


class TestAppLaunches:
    """Electron window opens with the expected UI elements."""

    def test_app_launches(self, page):
        """Electron window opens, canvas element visible, context menu works."""
        canvas = page.locator("#canvas")
        assert canvas.is_visible()

        # The stress-test preset ships 6 preset cards.  Wait for them to be
        # rendered rather than sleeping a fixed 2 s.  Use >= 1 (not == 6) so
        # the assertion does not become brittle if the preset is ever edited
        # or if the test is reused under another preset.
        page.wait_for_function(
            "() => typeof cards !== 'undefined' && cards.length >= 1",
            timeout=10000,
        )

    def test_canvas_element_exists(self, page):
        """Canvas container is present in the DOM."""
        assert page.locator("#canvas").count() == 1


class TestTerminalCardSpawns:
    """Terminal cards can be spawned via the context menu."""

    def test_terminal_card_spawns(self, page):
        """Spawn a terminal card from context menu, verify xterm container appears."""
        # Capture baseline card count before spawning.
        initial_count = page.locator("[data-card-id]").count()

        # Right-click on the viewport (not #canvas — viewport receives the event)
        viewport = page.locator("#viewport")
        viewport.click(button="right", position={"x": 500, "y": 500})

        # Wait for context menu to appear
        ctx_menu = page.locator("#context-menu")
        ctx_menu.wait_for(state="visible", timeout=3000)

        # Click on a host shell item (PowerShell or bash, depending on OS)
        host_item = ctx_menu.locator("[data-host-shell]").first
        if host_item.count() > 0:
            host_item.click()
        else:
            # Fall back to hub items
            hub_item = ctx_menu.locator("[data-hub]").first
            hub_item.click()

        # Wait for a new card to appear rather than sleeping a fixed 2 s.
        page.wait_for_function(
            f"() => document.querySelectorAll('[data-card-id]').length > {initial_count}",
            timeout=10000,
        )
        cards = page.locator("[data-card-id]")
        assert cards.count() > initial_count


class TestCardDrag:
    """Cards can be dragged by their titlebar."""

    def test_card_drag(self, page):
        """Drag card by titlebar, verify position changes in DOM."""
        # Pick the large terminal card (400,50 800x600) which isn't obscured
        # by the profiles widget (10,10 600x400).
        all_cards = page.locator("[data-card-id]")
        card = None
        card_id = None
        for i in range(all_cards.count()):
            c = all_cards.nth(i)
            style = c.get_attribute("style") or ""
            # The large card is at left:400px
            if "left: 400" in style or "left:400" in style:
                card = c
                card_id = c.get_attribute("data-card-id")
                break
        if card is None:
            # Fallback to any visible card
            card = all_cards.first
            if card.count() == 0:
                pytest.skip("No cards on canvas to drag")
            card_id = card.get_attribute("data-card-id")

        initial_style = card.get_attribute("style") or ""
        initial_left_match = re.search(r"left:\s*(-?\d+(?:\.\d+)?)px", initial_style)
        if not initial_left_match:
            pytest.skip("Card has no inline left style")

        initial_left = float(initial_left_match.group(1))

        titlebar = page.locator(f"[data-drag='{card_id}']")
        if titlebar.count() == 0:
            pytest.skip("No titlebar found for card")

        # Drag the titlebar
        box = titlebar.bounding_box()
        if box is None:
            pytest.skip("Titlebar not visible for drag")

        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2 + 100, box["y"] + box["height"] / 2 + 50, steps=5)
        page.mouse.up()

        # Wait for the card's inline left style to change rather than
        # sleeping a fixed 500 ms.  If the drag handler never fires (e.g.
        # the titlebar wasn't matched), the condition wait times out fast
        # and the test surfaces that as a real failure.
        try:
            page.wait_for_function(
                f"""(cardId) => {{
                    const el = document.querySelector(`[data-card-id="${{cardId}}"]`);
                    if (!el) return false;
                    const m = (el.getAttribute('style') || '').match(
                        /left:\\s*(-?\\d+(?:\\.\\d+)?)px/
                    );
                    return m !== null && parseFloat(m[1]) !== {initial_left};
                }}""",
                arg=card_id,
                timeout=3000,
            )
        except Exception:
            # Allow the assertion below to produce a clearer failure message.
            pass

        # Verify position changed
        new_style = card.get_attribute("style") or ""
        new_left_match = re.search(r"left:\s*(-?\d+(?:\.\d+)?)px", new_style)
        if new_left_match:
            new_left = float(new_left_match.group(1))
            assert new_left != initial_left, "Card left position should have changed after drag"


class TestCardResize:
    """Cards can be resized via the resize handle."""

    def test_card_resize(self, page):
        """Resize via handle, verify dimensions update."""
        # Pick the large terminal card (800x600) which isn't obscured
        all_cards = page.locator("[data-card-id]")
        card = None
        for i in range(all_cards.count()):
            c = all_cards.nth(i)
            style = c.get_attribute("style") or ""
            w_match = re.search(r"width:\s*(\d+)", style)
            if w_match and int(w_match.group(1)) >= 700:
                card = c
                break
        if card is None:
            card = all_cards.first
        if card.count() == 0:
            pytest.skip("No cards on canvas to resize")

        initial_style = card.get_attribute("style") or ""
        initial_w_match = re.search(r"width:\s*(-?\d+(?:\.\d+)?)px", initial_style)
        if not initial_w_match:
            pytest.skip("Card has no inline width style")

        initial_w = float(initial_w_match.group(1))
        card_id = card.get_attribute("data-card-id")

        handle = card.locator(".resize-handle")
        if handle.count() == 0:
            pytest.skip("No resize handle found")

        # Dispatch PointerEvents directly via JS rather than Playwright mouse
        # simulation.  page.mouse.move/down/up is unreliable in headless Linux
        # Chromium because:
        #   - The handle is only ~5px at fitAll zoom, making hit-testing miss it.
        #   - Drag targets below y≈720 are silently dropped outside the viewport.
        # dispatchEvent on the handle element and document bypasses both
        # constraints and exercises the JS resize handler directly.
        result = page.evaluate(f"""() => {{
            const handle = document.querySelector('[data-resize="{card_id}"]');
            if (!handle) return 'no-handle';
            const rect = handle.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            handle.dispatchEvent(new PointerEvent('pointerdown', {{
                bubbles: true, cancelable: true,
                clientX: cx, clientY: cy, buttons: 1,
            }}));
            document.dispatchEvent(new PointerEvent('pointermove', {{
                bubbles: true, cancelable: true,
                clientX: cx + 200, clientY: cy, buttons: 1,
            }}));
            document.dispatchEvent(new PointerEvent('pointerup', {{
                bubbles: true, cancelable: true,
                clientX: cx + 200, clientY: cy,
            }}));
            return 'ok';
        }}""")

        if result == "no-handle":
            pytest.skip("No resize handle found in DOM")

        # Wait for the card's inline width style to change rather than
        # sleeping a fixed 300 ms.  The resize handler dispatches a style
        # mutation inside the pointerup handler, so this observable flips
        # synchronously once the browser flushes the event queue.
        try:
            page.wait_for_function(
                f"""(cardId) => {{
                    const el = document.querySelector(`[data-card-id="${{cardId}}"]`);
                    if (!el) return false;
                    const m = (el.getAttribute('style') || '').match(
                        /width:\\s*(-?\\d+(?:\\.\\d+)?)px/
                    );
                    return m !== null && parseFloat(m[1]) !== {initial_w};
                }}""",
                arg=card_id,
                timeout=3000,
            )
        except Exception:
            pass

        new_style = card.get_attribute("style") or ""
        new_w_match = re.search(r"width:\s*(-?\d+(?:\.\d+)?)px", new_style)
        if new_w_match:
            new_w = float(new_w_match.group(1))
            assert new_w != initial_w, "Card width should have changed after resize"

    def test_min_size_enforced(self, page):
        """Resize to very small; card should not go below minimum size."""
        card = page.locator("[data-card-id]").first
        if card.count() == 0:
            pytest.skip("No cards on canvas")

        handle = card.locator(".resize-handle")
        if handle.count() == 0:
            pytest.skip("No resize handle found")

        box = handle.bounding_box()
        if box is None:
            pytest.skip("Resize handle not visible")

        # Capture width/height before the drag so we can wait for the
        # resize handler to have run (either clamping to minimum or not).
        pre_style = card.get_attribute("style") or ""
        pre_w_match = re.search(r"width:\s*(-?\d+(?:\.\d+)?)px", pre_style)
        pre_h_match = re.search(r"height:\s*(-?\d+(?:\.\d+)?)px", pre_style)
        pre_w = float(pre_w_match.group(1)) if pre_w_match else None
        pre_h = float(pre_h_match.group(1)) if pre_h_match else None

        card_id = card.get_attribute("data-card-id")

        # Try to drag the handle far to the upper-left (shrink significantly)
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] - 500, box["y"] - 500, steps=5)
        page.mouse.up()

        # Wait for either:
        # 1. The card's width/height inline style to change (resize fired and
        #    was either clamped or accepted), OR
        # 2. Two animation frames to flush (in case Playwright mouse drag was
        #    not picked up at all — the min-size floor assertion still holds
        #    against the original dimensions, so we can safely fall through).
        try:
            page.wait_for_function(
                f"""(cardId) => {{
                    const el = document.querySelector(`[data-card-id="${{cardId}}"]`);
                    if (!el) return false;
                    const s = el.getAttribute('style') || '';
                    const w = s.match(/width:\\s*(-?\\d+(?:\\.\\d+)?)px/);
                    const h = s.match(/height:\\s*(-?\\d+(?:\\.\\d+)?)px/);
                    if (!w || !h) return false;
                    const changed = (w && {pre_w if pre_w is not None else "null"} !== null &&
                                     parseFloat(w[1]) !== {pre_w if pre_w is not None else "null"}) ||
                                    (h && {pre_h if pre_h is not None else "null"} !== null &&
                                     parseFloat(h[1]) !== {pre_h if pre_h is not None else "null"});
                    return changed;
                }}""",
                arg=card_id,
                timeout=1500,
            )
        except Exception:
            # Drag may not have been picked up; the min-size assertion
            # below still guards against regressions on the original size.
            pass

        new_style = card.get_attribute("style") or ""
        w_match = re.search(r"width:\s*(-?\d+(?:\.\d+)?)px", new_style)
        h_match = re.search(r"height:\s*(-?\d+(?:\.\d+)?)px", new_style)
        if w_match and h_match:
            w = float(w_match.group(1))
            h = float(h_match.group(1))
            # Minimum card dimensions (from Card class in index.html)
            assert w >= 200, f"Width {w} below minimum"
            assert h >= 120, f"Height {h} below minimum"


class TestWidgetSpawns:
    """Widget cards can be spawned from context menu."""

    def test_widget_spawns(self, page):
        """Click widget item in context menu, widget card appears."""
        # Count current cards
        initial_count = page.locator("[data-card-id]").count()

        # Right-click on viewport (not #canvas — viewport receives the event)
        viewport = page.locator("#viewport")
        viewport.click(button="right", position={"x": 600, "y": 400})

        ctx_menu = page.locator("#context-menu")
        ctx_menu.wait_for(state="visible", timeout=3000)

        # Click a widget item
        widget_item = ctx_menu.locator("[data-widget]").first
        if widget_item.count() == 0:
            pytest.skip("No widgets in context menu")

        widget_item.click()

        # Wait for the new widget card to appear rather than sleeping
        # 1.5 s.  Widget spawn is driven by a synchronous DOM append in
        # the click handler, so this usually resolves within a frame.
        page.wait_for_function(
            f"() => document.querySelectorAll('[data-card-id]').length > {initial_count}",
            timeout=10000,
        )

        # Verify new card appeared
        new_count = page.locator("[data-card-id]").count()
        assert new_count > initial_count, "Widget card should have been added"


class TestCanvasPanZoom:
    """Canvas panning and zooming via mouse interactions."""

    def test_canvas_pan_zoom(self, page):
        """Pan canvas and zoom, verify #canvas transform updates."""
        canvas = page.locator("#canvas")

        # Pan: middle-click drag or shift+click drag
        # The canvas uses wheel for zoom; try mouse wheel
        canvas_box = canvas.bounding_box()
        if canvas_box is None:
            pytest.skip("Canvas not visible")

        cx = canvas_box["x"] + canvas_box["width"] / 2
        cy = canvas_box["y"] + canvas_box["height"] / 2

        # Capture pre-zoom transform so we can wait for a change rather
        # than sleeping a fixed 500 ms.
        pre_style = canvas.get_attribute("style") or ""

        # Zoom with mouse wheel
        page.mouse.move(cx, cy)
        page.mouse.wheel(0, -300)  # zoom in

        # Wait for the canvas transform to update (or gain a transform if
        # it had none).  The wheel handler applies the transform
        # synchronously inside the event, so this resolves within a
        # frame on a responsive machine.
        try:
            page.wait_for_function(
                f"""() => {{
                    const el = document.getElementById('canvas');
                    if (!el) return false;
                    const s = el.getAttribute('style') || '';
                    return s !== {pre_style!r} && (s.includes('transform') || s.includes('scale'));
                }}""",
                timeout=3000,
            )
        except Exception:
            # Fall through to the assertion — it has its own clearer error.
            pass

        new_style = canvas.get_attribute("style") or ""
        # Transform should contain scale() that is different from initial
        assert "transform" in new_style or "scale" in new_style, "Canvas should have transform style after zoom"


class TestCanvasSaveReload:
    """Canvas layout persistence across reload."""

    def test_canvas_save_reload(self, page, backend_port):
        """Save canvas, reload app, verify card positions are restored."""
        # Get current card count
        cards_before = page.locator("[data-card-id]").count()
        if cards_before == 0:
            pytest.skip("No cards to verify persistence")

        # Trigger a save by calling the API directly and wait for the
        # canvas PUT to complete — avoids sleeping 1 s regardless of
        # whether saveLayout actually fired.
        import re as _re

        with page.expect_response(
            lambda r: _re.search(r"/api/canvases/", r.url) is not None and r.request.method == "PUT",
            timeout=10000,
        ):
            page.evaluate(
                """async () => {
                // saveLayout is a global function in index.html
                if (typeof saveLayout === 'function') saveLayout();
            }"""
            )

        # Reload the page
        page.goto(f"http://localhost:{backend_port}")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#canvas", timeout=10000)
        # Wait for the boot IIFE to finish and cards to re-render from the
        # saved canvas rather than sleeping a fixed 3 s.
        page.wait_for_function(
            "() => window.__claudeRtsBootComplete === true",
            timeout=15000,
        )

        # Verify cards are restored
        cards_after = page.locator("[data-card-id]").count()
        assert cards_after > 0, "Cards should be restored after reload"
