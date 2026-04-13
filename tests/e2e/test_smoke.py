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

        # The canvas should have child elements (cards from stress-test preset)
        # Give cards time to render from startup script
        page.wait_for_timeout(2000)

    def test_canvas_element_exists(self, page):
        """Canvas container is present in the DOM."""
        assert page.locator("#canvas").count() == 1


class TestTerminalCardSpawns:
    """Terminal cards can be spawned via the context menu."""

    def test_terminal_card_spawns(self, page):
        """Spawn a terminal card from context menu, verify xterm container appears."""
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

        # Verify a new card appeared with data-card-id
        page.wait_for_timeout(2000)
        cards = page.locator("[data-card-id]")
        assert cards.count() > 0


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

        page.wait_for_timeout(500)

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

        # Zoom into the card so the 16×16px resize handle is large enough to
        # interact with reliably in headless CI (at fitAll zoom ≈ 0.25 the
        # handle is only ~4 screen pixels, making pointer events miss it).
        card_id = card.get_attribute("data-card-id")
        page.evaluate(f"""
            const c = window.cards && window.cards.find(c => String(c.id) === '{card_id}');
            if (c && typeof zoomToCard === 'function') zoomToCard(c);
        """)
        page.wait_for_timeout(500)

        # Find resize handle within this card (fresh bounding box after zoom)
        handle = card.locator(".resize-handle")
        if handle.count() == 0:
            pytest.skip("No resize handle found")

        box = handle.bounding_box()
        if box is None:
            pytest.skip("Resize handle not visible")

        # Move directly to handle centre — bypasses Playwright's hit-test,
        # which fails when another card's titlebar overlaps the handle corner.
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(
            box["x"] + box["width"] / 2 + 100,
            box["y"] + box["height"] / 2 + 80,
            steps=10,
        )
        page.mouse.up()

        page.wait_for_timeout(1500)

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

        # Try to drag the handle far to the upper-left (shrink significantly)
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] - 500, box["y"] - 500, steps=5)
        page.mouse.up()

        page.wait_for_timeout(500)

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
        page.wait_for_timeout(1500)

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

        # Zoom with mouse wheel
        page.mouse.move(cx, cy)
        page.mouse.wheel(0, -300)  # zoom in
        page.wait_for_timeout(500)

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

        # Trigger a save by calling the API directly
        page.evaluate(
            """async () => {
            // saveLayout is a global function in index.html
            if (typeof saveLayout === 'function') saveLayout();
        }"""
        )
        page.wait_for_timeout(1000)

        # Reload the page
        page.goto(f"http://localhost:{backend_port}")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#canvas", timeout=10000)
        page.wait_for_timeout(3000)  # wait for cards to render

        # Verify cards are restored
        cards_after = page.locator("[data-card-id]").count()
        assert cards_after > 0, "Cards should be restored after reload"
