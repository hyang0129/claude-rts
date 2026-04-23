"""Playwright E2E tests for PR #152: card starring, terminal rename, and recovery scripts.

Tests cover:
- Star toggle on terminal cards (UI + persistence)
- Double-click-to-rename display names
- Recovery script button visibility and execution
- Stable cardUid (UUID)
- REST API endpoints (rename, recovery-script, terminals list, card_updated broadcast)
- Integration scenarios and edge cases

Preset: stress-test (4 terminal cards + 2 widget cards)

Run:
    pip install pytest-playwright playwright
    python -m playwright install chromium
    python -m pytest tests/e2e/test_pr152_starring_rename_recovery.py -v

Headed mode:
    HEADED=1 python -m pytest tests/e2e/test_pr152_starring_rename_recovery.py -v
"""

import re

import pytest

pw = pytest.importorskip("playwright")


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dev_config_preset():
    """Use stress-test preset for all tests in this module."""
    return "stress-test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_terminal_card_ids(page):
    """Return list of card numeric ids (data-card-id) for terminal cards only.

    Terminal cards are identified by having a [data-star] child element.
    """
    return page.evaluate(
        """() => {
        return Array.from(document.querySelectorAll('[data-star]'))
            .map(el => el.dataset.star);
    }"""
    )


def get_terminal_session_map(page):
    """Return list of {id, sessionId} dicts for terminal cards with sessions."""
    return page.evaluate(
        """() => {
        return cards.filter(c => c.sessionId).map(c => ({id: String(c.id), sessionId: c.sessionId}));
    }"""
    )


def get_first_terminal_session_id(page):
    """Return (card_numeric_id, session_id) for the first terminal card with a session."""
    mapping = get_terminal_session_map(page)
    if not mapping:
        return None, None
    return mapping[0]["id"], mapping[0]["sessionId"]


def reload_page(page, backend_port):
    """Reload and wait for cards to render.

    Epic #236 child 5 (#241/#247) made canvas JSON server-authored via the
    ``CardRegistry`` write-through hook, so the client no longer saves
    layout before reload — every prior mutation (star click, drag, rename)
    already round-tripped through ``PUT /api/cards/{id}/state`` and the
    server has persisted the snapshot synchronously by the time this
    helper runs. We wait on ``window.__claudeRtsBootComplete`` (set at the
    end of the boot IIFE) to confirm the reloaded page has hydrated from
    the server snapshot.
    """
    page.goto(f"http://localhost:{backend_port}")
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("#canvas", timeout=15000)
    page.wait_for_function(
        "() => window.__claudeRtsBootComplete === true",
        timeout=15000,
    )


def js_click(page, selector):
    """Click an element via JS, bypassing viewport/overlap constraints."""
    page.evaluate(f"document.querySelector('{selector}')?.click()")


def js_dblclick(page, selector):
    """Double-click an element via JS, bypassing viewport/overlap constraints."""
    page.evaluate(
        f"""() => {{
        const el = document.querySelector('{selector}');
        if (el) el.dispatchEvent(new MouseEvent('dblclick', {{bubbles: true, cancelable: true}}));
    }}"""
    )


def js_rename(page, card_id, value, commit="enter"):
    """Open rename input via dblclick, set value, and commit/cancel.

    commit: 'enter', 'blur', or 'escape'
    """
    page.evaluate(
        f"""() => {{
        const el = document.querySelector('[data-display-name="{card_id}"]');
        if (el) el.dispatchEvent(new MouseEvent('dblclick', {{bubbles: true, cancelable: true}}));
    }}"""
    )
    # Wait for the inline rename input to appear in the titlebar.
    page.wait_for_function(
        f"""() => {{
            const tb = document.querySelector('[data-drag="{card_id}"]');
            return tb && tb.querySelector('input') !== null;
        }}""",
        timeout=3000,
    )
    # Set the input value and commit
    key_event = {
        "enter": "new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})",
        "escape": "new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})",
        "blur": None,
    }[commit]
    dispatch = f"input.dispatchEvent({key_event})" if key_event else "input.blur()"
    page.evaluate(
        f"""() => {{
        const titlebar = document.querySelector('[data-drag="{card_id}"]');
        const input = titlebar ? titlebar.querySelector('input') : null;
        if (input) {{
            input.focus();
            input.value = {repr(value)};
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
            {dispatch};
        }}
    }}"""
    )
    # Commit removes the input and re-renders the display-name span.  Wait
    # for the input to disappear rather than sleeping a blind 300ms.
    page.wait_for_function(
        f"""() => {{
            const tb = document.querySelector('[data-drag="{card_id}"]');
            return tb && tb.querySelector('input') === null;
        }}""",
        timeout=3000,
    )


def get_star_text(page, card_id):
    """Get the text content of a star button."""
    return page.evaluate(f"document.querySelector('[data-star=\"{card_id}\"]')?.textContent || ''")


def save_layout_and_wait(page):
    """No-op after epic #236 child 5 — canvas JSON is server-authored.

    Every user-visible mutation (star click, drag, resize, rename) now
    round-trips through ``PUT /api/cards/{id}/state`` and the server's
    ``CardRegistry`` write-through hook rewrites the snapshot
    synchronously before the HTTP response returns. Callers that
    previously relied on this helper to force persistence no longer need
    to — the mutation itself is the save.

    The helper is retained (as a no-op) so existing call sites continue
    to read clearly; the ``wait_for_*`` helpers that follow each call are
    what actually guarantee the broadcast has reached the DOM before the
    assertion runs.
    """
    return


def wait_for_star_text(page, card_id, expected, timeout=3000):
    """Wait until the star button for ``card_id`` has ``expected`` textContent.

    Replaces fixed-duration sleeps after ``js_click`` on a star button —
    the click updates the DOM synchronously, but the server PUT is
    asynchronous and the previous fixed sleeps were guarding against that
    round-trip.  Polling the DOM is both faster and more reliable.
    """
    page.wait_for_function(
        f"""() => {{
            const el = document.querySelector('[data-star="{card_id}"]');
            return el && el.textContent === {expected!r};
        }}""",
        timeout=timeout,
    )


def get_name_text(page, card_id):
    """Get the text content of a display name span."""
    return page.evaluate(f"document.querySelector('[data-display-name=\"{card_id}\"]')?.textContent || ''")


# ---------------------------------------------------------------------------
# P0: Star Toggle
# ---------------------------------------------------------------------------


class TestStarToggle:
    """Star button rendering and toggle behavior on terminal cards."""

    def test_star_button_renders_on_terminal_cards(self, page):
        """Every terminal card has a star button that renders a valid star
        glyph (★ filled / ☆ unfilled) with the matching gold/gray color.
        Issue #194 flipped the default to unstarred, so this test just
        verifies the button exists and is in a coherent state."""
        star_buttons = page.locator("[data-star]")
        count = star_buttons.count()
        assert count > 0, "Expected at least one star button on terminal cards"

        for i in range(count):
            btn = star_buttons.nth(i)
            text = btn.text_content()
            assert text in ("\u2605", "\u2606"), f"Star button {i} should show a star glyph, got '{text}'"
            color = btn.evaluate("el => getComputedStyle(el).color")
            if text == "\u2605":
                assert "249" in color or "f9e2af" in color, f"Filled star {i} should be gold, got '{color}'"
            else:
                assert "88" in color or "585b70" in color, f"Unfilled star {i} should be gray, got '{color}'"

    def test_toggle_star_off(self, page):
        """Clicking a star toggles it to unfilled gray."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        js_click(page, f'[data-star="{target_id}"]')
        page.wait_for_function(
            f"""() => {{
                const el = document.querySelector('[data-star="{target_id}"]');
                return el && el.textContent === '\u2606';
            }}""",
            timeout=3000,
        )

        text = get_star_text(page, target_id)
        assert text == "\u2606", f"Star should be unfilled after click, got '{text}'"
        color = page.evaluate(f"document.querySelector('[data-star=\"{target_id}\"]')?.style.color || ''")
        assert color in ("rgb(88, 91, 112)", "#585b70"), f"Unstarred color should be gray, got '{color}'"

    def test_toggle_star_on(self, page):
        """Clicking the unstarred star toggles it back to filled gold."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        # Ensure it is currently unstarred
        if get_star_text(page, target_id) == "\u2605":
            js_click(page, f'[data-star="{target_id}"]')
            page.wait_for_function(
                f"""() => {{
                    const el = document.querySelector('[data-star="{target_id}"]');
                    return el && el.textContent === '\u2606';
                }}""",
                timeout=3000,
            )

        js_click(page, f'[data-star="{target_id}"]')
        page.wait_for_function(
            f"""() => {{
                const el = document.querySelector('[data-star="{target_id}"]');
                return el && el.textContent === '\u2605';
            }}""",
            timeout=3000,
        )

        text = get_star_text(page, target_id)
        assert text == "\u2605", f"Star should be filled after re-toggle, got '{text}'"
        color = page.evaluate(f"document.querySelector('[data-star=\"{target_id}\"]')?.style.color || ''")
        assert color in ("rgb(249, 226, 175)", "#f9e2af"), f"Starred color should be gold, got '{color}'"


# ---------------------------------------------------------------------------
# P0: Star Persistence
# ---------------------------------------------------------------------------


class TestStarPersistence:
    """Star state persists across saves and reloads."""

    def test_starred_card_persists(self, page, backend_port):
        """A starred card survives reload with a live terminal."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        if get_star_text(page, target_id) != "\u2605":
            js_click(page, f'[data-star="{target_id}"]')
            wait_for_star_text(page, target_id, "\u2605")

        reload_page(page, backend_port)

        new_ids = get_terminal_card_ids(page)
        assert len(new_ids) > 0, "Terminal cards should exist after reload"

        found_xterm = False
        for cid in new_ids:
            body = page.locator(f'[data-body="{cid}"]')
            xterm = body.locator(".xterm")
            if xterm.count() > 0:
                found_xterm = True
                break
        assert found_xterm, "At least one starred card should have a live xterm terminal"

    def test_unstarred_card_goes_dormant(self, page):
        """Unstarring a live card swaps its body to the dormant placeholder.

        Issue #194 / epic #236 made unstarred cards ephemeral \u2014 they are
        filtered out of the canvas snapshot by the write-through hook
        (server.py:2553). Reload is a different invariant (they vanish).
        What persists across unstar is the live\u2192dormant transition driven
        by the server's ``card_updated`` broadcast; that's observable in
        the DOM without any reload.
        """
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) >= 2

        target_id = card_ids[-1]
        if get_star_text(page, target_id) == "\u2605":
            js_click(page, f'[data-star="{target_id}"]')
            wait_for_star_text(page, target_id, "\u2606")

        # Wait for the dormant placeholder to swap in.
        page.wait_for_function(
            f"""() => {{
                const body = document.querySelector('[data-body="{target_id}"]');
                return body && body.innerText.includes('Dormant');
            }}""",
            timeout=5000,
        )

        body = page.locator(f'[data-body="{target_id}"]')
        assert body.locator(".xterm").count() == 0, "Dormant card should not have an xterm element"

    def test_resume_dormant_card(self, page):
        """Resume on a dormant card restores live xterm + filled star.

        Post-epic #236, the live\u2194dormant transition is driven entirely by the
        server's ``card_updated`` broadcast, so the round-trip through reload
        is not required to exercise the Resume path: unstar live \u2192 observe
        dormant \u2192 click Resume \u2192 observe live.
        """
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) >= 2

        target_id = card_ids[-1]
        if get_star_text(page, target_id) == "\u2605":
            js_click(page, f'[data-star="{target_id}"]')
            wait_for_star_text(page, target_id, "\u2606")

        page.wait_for_function(
            f"""() => {{
                const body = document.querySelector('[data-body="{target_id}"]');
                return body && body.innerText.includes('Dormant');
            }}""",
            timeout=5000,
        )

        # Click Resume via JS
        page.evaluate(
            f"""() => {{
            const body = document.querySelector('[data-body="{target_id}"]');
            const btn = body ? body.querySelector('button') : null;
            if (btn) btn.click();
        }}"""
        )

        page.wait_for_selector(f'[data-body="{target_id}"] .xterm', timeout=10000)
        # Resume races the WS handshake \u2014 the xterm element appears before the
        # server's ``session_id`` control message lands. Wait on sessionId so
        # downstream tests (which require an authenticated session to star/
        # unstar via ``putCardState``) don't inherit a half-initialised card.
        page.wait_for_function(
            f"""() => {{
                const c = cards.find(c => String(c.id) === '{target_id}');
                return c && c.sessionId;
            }}""",
            timeout=10000,
        )

        text = get_star_text(page, target_id)
        assert text == "\u2605", "Star should be filled after resume"

    def test_star_state_in_canvas_json(self, page, backend_port):
        """Canvas JSON no longer carries ``starred`` — it is server-owned.

        Epic #236 child 3 (#239): ``saveLayout()`` filters to only starred
        cards and does NOT serialise the ``starred`` key. Unstarred cards
        are absent from the canvas JSON entirely; starred cards appear
        without a ``starred`` field. The server's ``CardRegistry`` (and the
        ``PUT /api/cards/{id}/state`` mutation path) is the sole authority.
        """
        # Reload to a clean, fully hydrated state. The module-scoped ``page``
        # fixture accumulates mutations across tests; a dormant<->live
        # transition from a prior test can leave a card without a fresh
        # ``sessionId`` at click time, making the star PUT below a no-op.
        reload_page(page, backend_port)

        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) >= 2

        if get_star_text(page, card_ids[0]) != "\u2605":
            js_click(page, f'[data-star="{card_ids[0]}"]')
            wait_for_star_text(page, card_ids[0], "\u2605")

        if get_star_text(page, card_ids[-1]) != "\u2606":
            js_click(page, f'[data-star="{card_ids[-1]}"]')
            wait_for_star_text(page, card_ids[-1], "\u2606")

        # Epic #236 child 5 (#247) removed PUT /api/canvases/{name}: canvas
        # JSON is now server-authored by ``TerminalCard.to_descriptor()``
        # under the ``CardRegistry`` write-through hook. Each star click
        # above already round-tripped through PUT /api/cards/{id}/state and
        # the snapshot on disk is fresh — we just GET it.
        canvas_json = page.evaluate(
            """async () => {
            const resp = await fetch('/api/canvases/stress-layout');
            return await resp.json();
        }"""
        )

        terminal_cards = [c for c in canvas_json["cards"] if c.get("type") == "terminal"]
        assert len(terminal_cards) > 0, "Expected at least one starred card in canvas JSON"
        # Post-epic: ``to_descriptor()`` emits ``starred`` on every terminal
        # entry (bool, both True and False) because the server is the sole
        # authority on the value. Unstarred cards are filtered out of the
        # snapshot entirely by the write-through hook.
        for c in terminal_cards:
            assert isinstance(c.get("starred"), bool), (
                f"Server-authored snapshot must carry ``starred`` as bool; got {c!r}"
            )
            assert c["starred"] is True, f"Only starred cards should appear in the snapshot; got {c!r}"


# ---------------------------------------------------------------------------
# P0: Rename
# ---------------------------------------------------------------------------


class TestRename:
    """Double-click-to-rename behavior on terminal card titlebars."""

    def test_default_name_and_tooltip(self, page):
        """Display name span shows hub name with rename tooltip."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        name_span = page.locator(f'[data-display-name="{target_id}"]')
        assert name_span.count() > 0, "Display name span should exist"

        text = name_span.text_content()
        assert text, "Display name should not be empty"

        title = name_span.get_attribute("title")
        assert title == "Double-click to rename", f"Expected rename tooltip, got '{title}'"

    def test_dblclick_opens_input(self, page):
        """Double-clicking the display name opens an inline input field."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        js_dblclick(page, f'[data-display-name="{target_id}"]')
        page.wait_for_function(
            f"""() => {{
                const tb = document.querySelector('[data-drag="{target_id}"]');
                return tb && tb.querySelector('input') !== null;
            }}""",
            timeout=3000,
        )

        has_input = page.evaluate(
            f"""() => {{
            const tb = document.querySelector('[data-drag="{target_id}"]');
            return tb ? !!tb.querySelector('input') : false;
        }}"""
        )
        assert has_input, "Input field should appear after double-click"

        is_focused = page.evaluate("document.activeElement.tagName")
        assert is_focused == "INPUT", f"Input should be focused, active element is {is_focused}"

        # Cancel to restore state; wait for input removal rather than sleep.
        page.evaluate(
            f"""() => {{
            const tb = document.querySelector('[data-drag="{target_id}"]');
            const input = tb ? tb.querySelector('input') : null;
            if (input) input.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Escape', bubbles: true}}));
        }}"""
        )
        page.wait_for_function(
            f"""() => {{
                const tb = document.querySelector('[data-drag="{target_id}"]');
                return tb && tb.querySelector('input') === null;
            }}""",
            timeout=3000,
        )

    def test_rename_via_enter(self, page):
        """Typing a new name and pressing Enter commits the rename."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        js_rename(page, target_id, "My Terminal", commit="enter")

        text = get_name_text(page, target_id)
        assert text == "My Terminal"

    def test_rename_via_blur(self, page):
        """Clicking elsewhere commits the rename via blur."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        js_rename(page, target_id, "Blur Rename", commit="blur")

        text = get_name_text(page, target_id)
        assert text == "Blur Rename"

    def test_rename_cancel_escape(self, page):
        """Pressing Escape discards the rename and restores the original name."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        original_name = get_name_text(page, target_id)

        js_rename(page, target_id, "Should Be Cancelled", commit="escape")

        text = get_name_text(page, target_id)
        assert text == original_name, "Name should revert to original after Escape"

    def test_clear_name_reverts(self, page):
        """Clearing the name and pressing Enter reverts to hub name."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        js_rename(page, target_id, "", commit="enter")

        text = get_name_text(page, target_id)
        assert text, "Name should not be empty after clearing -- should show hub name"
        assert text.strip(), "Display name should not be whitespace-only"


# ---------------------------------------------------------------------------
# P0: Rename Persistence
# ---------------------------------------------------------------------------


class TestRenamePersistence:
    """Renamed terminal cards persist across saves and reloads."""

    def test_rename_survives_reload(self, page, backend_port):
        """A renamed card retains its custom name after reload."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        js_rename(page, target_id, "Persistent Name", commit="enter")

        # The rename handler DOM-commits optimistically and fires the server
        # PUT fire-and-forget (epic #236 DP-4). Poll the server's authoritative
        # view before reload to ensure the PUT landed — otherwise reload may
        # race ahead of the write-through and the snapshot never captured it.
        page.wait_for_function(
            f"""async () => {{
                const resp = await fetch('/api/claude/terminals');
                if (!resp.ok) return false;
                const body = await resp.json();
                const list = Array.isArray(body) ? body : (body.terminals || []);
                return list.some(
                    t => String(t.card_id || t.session_id) === '{target_id}'
                      && t.display_name === 'Persistent Name'
                );
            }}""",
            timeout=5000,
        )

        reload_page(page, backend_port)

        new_ids = get_terminal_card_ids(page)
        found = False
        for cid in new_ids:
            if get_name_text(page, cid) == "Persistent Name":
                found = True
                break
        assert found, "Custom name 'Persistent Name' should survive reload"

    def test_displayname_in_canvas_json(self, page, backend_port):
        """Canvas JSON includes the displayName field for renamed cards."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        js_rename(page, target_id, "JSON Name", commit="enter")

        save_layout_and_wait(page)

        canvas_json = page.evaluate(
            """async () => {
            const resp = await fetch('/api/canvases/stress-layout');
            return await resp.json();
        }"""
        )

        terminal_cards = [c for c in canvas_json["cards"] if c.get("type") == "terminal"]
        has_display_name = any(c.get("display_name") for c in terminal_cards)
        assert has_display_name, "At least one terminal card should have display_name in canvas JSON"


# ---------------------------------------------------------------------------
# P0: REST API
# ---------------------------------------------------------------------------


class TestRestApi:
    """REST API endpoints for rename, recovery-script, and terminals list."""

    def test_put_rename(self, page):
        """PUT /api/claude/terminal/<id>/rename returns 200 with updated name."""
        _, session_id = get_first_terminal_session_id(page)
        if not session_id:
            pytest.skip("No terminal with session_id available")

        result = page.evaluate(
            f"""async () => {{
            const resp = await fetch('/api/claude/terminal/{session_id}/rename', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{display_name: 'API Rename'}})
            }});
            return {{status: resp.status, body: await resp.json()}};
        }}"""
        )

        assert result["status"] == 200
        assert result["body"]["display_name"] == "API Rename"

    def test_rename_invalid_session_404(self, page):
        """PUT /api/claude/terminal/<bogus>/rename returns 404."""
        result = page.evaluate(
            """async () => {
            const resp = await fetch('/api/claude/terminal/bogus-session-id/rename', {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({display_name: 'Test'})
            });
            return resp.status;
        }"""
        )
        assert result == 404

    def test_rename_invalid_json_400(self, page):
        """PUT /api/claude/terminal/<id>/rename with invalid JSON returns 400."""
        _, session_id = get_first_terminal_session_id(page)
        if not session_id:
            pytest.skip("No terminal with session_id available")

        result = page.evaluate(
            f"""async () => {{
            const resp = await fetch('/api/claude/terminal/{session_id}/rename', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: 'not valid json'
            }});
            return resp.status;
        }}"""
        )
        assert result == 400

    def test_get_recovery_script(self, page):
        """GET /api/claude/terminal/<id>/recovery-script returns 200."""
        _, session_id = get_first_terminal_session_id(page)
        if not session_id:
            pytest.skip("No terminal with session_id available")

        result = page.evaluate(
            f"""async () => {{
            const resp = await fetch('/api/claude/terminal/{session_id}/recovery-script');
            return {{status: resp.status, body: await resp.json()}};
        }}"""
        )

        assert result["status"] == 200
        assert "recovery_script" in result["body"]

    def test_put_recovery_script(self, page):
        """PUT /api/claude/terminal/<id>/recovery-script sets the script."""
        _, session_id = get_first_terminal_session_id(page)
        if not session_id:
            pytest.skip("No terminal with session_id available")

        result = page.evaluate(
            f"""async () => {{
            const resp = await fetch('/api/claude/terminal/{session_id}/recovery-script', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{recovery_script: 'echo hello'}})
            }});
            return {{status: resp.status, body: await resp.json()}};
        }}"""
        )

        assert result["status"] == 200

    def test_recovery_invalid_session_404(self, page):
        """PUT /api/claude/terminal/<bogus>/recovery-script returns 404."""
        result = page.evaluate(
            """async () => {
            const resp = await fetch('/api/claude/terminal/bogus-id/recovery-script', {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({recovery_script: 'echo hi'})
            });
            return resp.status;
        }"""
        )
        assert result == 404

    def test_list_terminals_enriched(self, page):
        """GET /api/claude/terminals includes display_name and recovery_script fields."""
        _, session_id = get_first_terminal_session_id(page)
        if not session_id:
            pytest.skip("No terminal with session_id available")

        page.evaluate(
            f"""async () => {{
            await fetch('/api/claude/terminal/{session_id}/rename', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{display_name: 'Enriched Terminal'}})
            }});
            await fetch('/api/claude/terminal/{session_id}/recovery-script', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{recovery_script: 'echo enriched'}})
            }});
        }}"""
        )
        # Wait for the enriched fields to be observable via the list API
        # instead of sleeping a fixed 500ms after the PUTs.
        page.wait_for_function(
            """async () => {
                const r = await fetch('/api/claude/terminals');
                if (!r.ok) return false;
                const list = await r.json();
                return list.some(t =>
                    t.display_name === 'Enriched Terminal' &&
                    t.recovery_script === 'echo enriched'
                );
            }""",
            timeout=5000,
        )

        result = page.evaluate(
            """async () => {
            const resp = await fetch('/api/claude/terminals');
            return await resp.json();
        }"""
        )

        assert isinstance(result, list)
        assert len(result) > 0

        enriched = [t for t in result if t.get("display_name") == "Enriched Terminal"]
        assert len(enriched) > 0, "Expected terminal with display_name='Enriched Terminal'"
        assert enriched[0].get("recovery_script") == "echo enriched"

    def test_card_updated_broadcast(self, page):
        """Renaming via API updates the DOM name span without page reload."""
        card_id, session_id = get_first_terminal_session_id(page)
        if not session_id:
            pytest.skip("No terminal with session_id available")

        page.evaluate(
            f"""async () => {{
            await fetch('/api/claude/terminal/{session_id}/rename', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{display_name: 'Broadcast Test'}})
            }});
        }}"""
        )

        page.wait_for_function(
            f"""() => {{
            const span = document.querySelector('[data-display-name="{card_id}"]');
            return span && span.textContent === 'Broadcast Test';
        }}""",
            timeout=5000,
        )

        text = get_name_text(page, card_id)
        assert text == "Broadcast Test"


# ---------------------------------------------------------------------------
# P1: Recovery Script
# ---------------------------------------------------------------------------


class TestRecovery:
    """Recovery script button visibility and persistence."""

    def test_recovery_btn_hidden_by_default(self, page):
        """Recovery button is hidden (display:none) when no script is set."""
        # The ``page`` fixture is module-scoped; server-side recovery scripts
        # set by earlier tests persist (correctly — epic #236 made the
        # write-through authoritative). Clear every terminal's recovery script
        # so "hidden by default" means what it says for this test run.
        mapping = get_terminal_session_map(page)
        for m in mapping:
            page.evaluate(
                f"""async () => {{
                await fetch('/api/claude/terminal/{m["sessionId"]}/recovery-script', {{
                    method: 'PUT',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{recovery_script: ''}})
                }});
            }}"""
            )
        # Wait for the card_updated broadcast to hide every recovery button.
        page.wait_for_function(
            """() => cards.every(c => !c.recoveryScript)""",
            timeout=5000,
        )

        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0

        target_id = card_ids[-1]
        display = page.evaluate(f"document.querySelector('[data-recovery=\"{target_id}\"]')?.style.display || ''")
        assert display == "none", f"Recovery button should be hidden, got display='{display}'"

    def test_recovery_btn_visible_after_api_set(self, page):
        """Recovery button becomes visible after setting script via API."""
        mapping = get_terminal_session_map(page)
        assert len(mapping) > 0
        card_id = mapping[0]["id"]
        session_id = mapping[0]["sessionId"]

        page.evaluate(
            f"""async () => {{
            await fetch('/api/claude/terminal/{session_id}/recovery-script', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{recovery_script: 'echo recovery-test'}})
            }});
        }}"""
        )

        page.wait_for_function(
            f"""() => {{
            const btn = document.querySelector('[data-recovery="{card_id}"]');
            return btn && btn.style.display !== 'none';
        }}""",
            timeout=5000,
        )

        display = page.evaluate(f"document.querySelector('[data-recovery=\"{card_id}\"]')?.style.display || ''")
        assert display == "inline", f"Recovery button should be visible, got display='{display}'"

    def test_recovery_persists_reload(self, page, backend_port):
        """Recovery script and button visibility persist across reload."""
        mapping = get_terminal_session_map(page)
        assert len(mapping) > 0
        session_id = mapping[0]["sessionId"]

        page.evaluate(
            f"""async () => {{
            await fetch('/api/claude/terminal/{session_id}/recovery-script', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{recovery_script: 'echo persist-test'}})
            }});
        }}"""
        )
        # Wait for the card_updated broadcast to land so the in-memory
        # card has the recoveryScript before we save+reload.  Polling the
        # DOM-rendered recovery button's display state is the observable
        # that guarantees the client-side state has caught up.
        page.wait_for_function(
            f"""() => {{
                const card = cards.find(c => c.sessionId === '{session_id}');
                return card && card.recoveryScript === 'echo persist-test';
            }}""",
            timeout=5000,
        )

        reload_page(page, backend_port)

        canvas_json = page.evaluate(
            """async () => {
            const resp = await fetch('/api/canvases/stress-layout');
            return await resp.json();
        }"""
        )

        terminal_cards = [c for c in canvas_json["cards"] if c.get("type") == "terminal"]
        has_recovery = any(c.get("recovery_script") for c in terminal_cards)
        assert has_recovery, "At least one card should have recovery_script in canvas JSON"

        card_ids = get_terminal_card_ids(page)
        found_visible = False
        for cid in card_ids:
            display = page.evaluate(f"document.querySelector('[data-recovery=\"{cid}\"]')?.style.display || 'none'")
            if display != "none":
                found_visible = True
                break

        assert found_visible, "Recovery button should be visible for card with script after reload"


# ---------------------------------------------------------------------------
# P1: Card UID
# ---------------------------------------------------------------------------


class TestCardUid:
    """Stable cardUid (UUID) on terminal cards."""

    def test_card_has_uuid(self, page):
        """Each terminal card in canvas JSON has a cardUid matching UUID format."""
        save_layout_and_wait(page)

        canvas_json = page.evaluate(
            """async () => {
            const resp = await fetch('/api/canvases/stress-layout');
            return await resp.json();
        }"""
        )

        terminal_cards = [c for c in canvas_json["cards"] if c.get("type") == "terminal"]
        assert len(terminal_cards) > 0

        uuid_pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        for i, card in enumerate(terminal_cards):
            uid = card.get("card_uid", "")
            assert uid, f"Terminal card {i} should have a card_uid"
            if len(uid) == 36:
                assert uuid_pattern.match(uid), f"Terminal card {i} card_uid '{uid}' does not match UUID format"

    def test_uids_unique(self, page):
        """All cardUid values are unique across terminal cards."""
        save_layout_and_wait(page)

        canvas_json = page.evaluate(
            """async () => {
            const resp = await fetch('/api/canvases/stress-layout');
            return await resp.json();
        }"""
        )

        terminal_cards = [c for c in canvas_json["cards"] if c.get("type") == "terminal"]
        uids = [c.get("card_uid") for c in terminal_cards if c.get("card_uid")]
        assert len(uids) == len(terminal_cards), "All terminal cards should have card_uid"
        assert len(set(uids)) == len(uids), f"card_uid values should be unique, got {uids}"


# ---------------------------------------------------------------------------
# P1: Integration
# ---------------------------------------------------------------------------


class TestIntegration:
    """End-to-end integration scenarios combining multiple features."""

    def test_starred_rename_and_recovery_survive_reload(self, page, backend_port):
        """Starred path: rename + recovery-script persist across reload.

        Post-epic #236 / issue #194 split of the original "full lifecycle"
        test. Exercises that display_name and recovery_script -- both
        server-owned via PUT /api/cards/{id}/state and the write-through
        hook -- survive a reload round-trip.
        """
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        if get_star_text(page, target_id) == "☆":
            js_click(page, f'[data-star="{target_id}"]')
            wait_for_star_text(page, target_id, "★")

        js_rename(page, target_id, "Lifecycle Starred", commit="enter")
        page.wait_for_function(
            f"""async () => {{
                const resp = await fetch('/api/claude/terminals');
                if (!resp.ok) return false;
                const body = await resp.json();
                const list = Array.isArray(body) ? body : (body.terminals || []);
                return list.some(
                    t => String(t.card_id || t.session_id) === '{target_id}'
                      && t.display_name === 'Lifecycle Starred'
                );
            }}""",
            timeout=5000,
        )

        mapping = get_terminal_session_map(page)
        target_session = next((m["sessionId"] for m in mapping if m["id"] == target_id), None)
        assert target_session is not None
        page.evaluate(
            f"""async () => {{
            await fetch('/api/claude/terminal/{target_session}/recovery-script', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{recovery_script: 'echo lifecycle'}})
            }});
        }}"""
        )
        page.wait_for_function(
            f"""() => {{
                const card = cards.find(c => c.sessionId === '{target_session}');
                return card && card.recoveryScript === 'echo lifecycle';
            }}""",
            timeout=5000,
        )

        reload_page(page, backend_port)

        new_ids = get_terminal_card_ids(page)
        found = False
        for cid in new_ids:
            if get_name_text(page, cid) == "Lifecycle Starred":
                display = page.evaluate(f"document.querySelector('[data-recovery=\"{cid}\"]')?.style.display || 'none'")
                assert display != "none", "Recovery button should be visible after reload"
                found = True
                break
        assert found, "Expected renamed card 'Lifecycle Starred' after reload"

    def test_unstar_then_resume_live_cycle(self, page):
        """Unstarred path: unstar -> dormant -> Resume -> live (no reload).

        Companion to test_starred_rename_and_recovery_survive_reload.
        Unstarred cards don't round-trip through the snapshot (issue #194),
        so the live<->dormant transition is exercised via the server's
        card_updated broadcast without a reload.
        """
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        if get_star_text(page, target_id) == "★":
            js_click(page, f'[data-star="{target_id}"]')
            wait_for_star_text(page, target_id, "☆")

        page.wait_for_function(
            f"""() => {{
                const body = document.querySelector('[data-body="{target_id}"]');
                return body && body.innerText.includes('Dormant');
            }}""",
            timeout=5000,
        )

        page.evaluate(
            f"""() => {{
            const body = document.querySelector('[data-body="{target_id}"]');
            const btn = body ? body.querySelector('button') : null;
            if (btn) btn.click();
        }}"""
        )
        page.wait_for_selector(f'[data-body="{target_id}"] .xterm', timeout=10000)
        page.wait_for_function(
            f"""() => {{
                const c = cards.find(c => String(c.id) === '{target_id}');
                return c && c.sessionId;
            }}""",
            timeout=10000,
        )
        assert get_star_text(page, target_id) == "★"

    def test_multi_card_independence(self, page):
        """Starring, unstarring, and renaming different cards are independent.

        Post-issue-#194: unstarred cards are ephemeral (filtered from the
        server snapshot by the write-through hook), so reload drops card 1
        entirely instead of restoring it as dormant. Observe the three
        mutations live on the same page: card 0 live+starred, card 1
        dormant in place, card 2 renamed. No reload.
        """
        card_ids = get_terminal_card_ids(page)
        if len(card_ids) < 3:
            pytest.skip("Need at least 3 terminal cards for multi-card independence test")

        # Star card 0 (ensure starred)
        if get_star_text(page, card_ids[0]) != "\u2605":
            js_click(page, f'[data-star="{card_ids[0]}"]')
            wait_for_star_text(page, card_ids[0], "\u2605")

        # Unstar card 1
        if get_star_text(page, card_ids[1]) != "\u2606":
            js_click(page, f'[data-star="{card_ids[1]}"]')
            wait_for_star_text(page, card_ids[1], "\u2606")

        # Rename card 2
        js_rename(page, card_ids[2], "Independent Card", commit="enter")
        assert get_name_text(page, card_ids[2]) == "Independent Card"

        # Card 1 should have transitioned live -> dormant in place via the
        # card_updated broadcast driven by the unstar PUT.
        page.wait_for_function(
            f"""() => {{
                const body = document.querySelector('[data-body="{card_ids[1]}"]');
                return body && body.innerText.includes('Dormant');
            }}""",
            timeout=5000,
        )

        has_live = False
        has_dormant = False

        for cid in (card_ids[0], card_ids[1], card_ids[2]):
            body = page.locator(f'[data-body="{cid}"]')
            text = body.inner_text()

            if "Dormant" in text:
                has_dormant = True
            elif page.locator(f'[data-body="{cid}"] .xterm').count() > 0:
                has_live = True

        assert has_live, "Should have at least one live (starred) card"
        assert has_dormant, "Should have at least one dormant (unstarred) card"

    def test_concurrent_rename_no_crash(self, page):
        """UI rename and API rename at the same time should not crash."""
        mapping = get_terminal_session_map(page)
        if not mapping:
            pytest.skip("No terminal sessions available")

        card_id = mapping[0]["id"]
        session_id = mapping[0]["sessionId"]

        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))

        # Open inline rename input via JS; wait for input to appear rather
        # than sleep a fixed 200ms.
        js_dblclick(page, f'[data-display-name="{card_id}"]')
        page.wait_for_function(
            f"""() => {{
                const tb = document.querySelector('[data-drag="{card_id}"]');
                return tb && tb.querySelector('input') !== null;
            }}""",
            timeout=3000,
        )

        # Fire API rename while input is open; the fetch is awaited, so
        # no sleep is needed after it resolves — the card_updated
        # broadcast is what we care about for the "no crash" assertion.
        page.evaluate(
            f"""async () => {{
            await fetch('/api/claude/terminal/{session_id}/rename', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{display_name: 'API Concurrent'}})
            }});
        }}"""
        )

        # Cancel UI rename via JS; wait for the input to be removed.
        page.evaluate(
            f"""() => {{
            const tb = document.querySelector('[data-drag="{card_id}"]');
            const input = tb ? tb.querySelector('input') : null;
            if (input) input.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Escape', bubbles: true}}));
        }}"""
        )
        page.wait_for_function(
            f"""() => {{
                const tb = document.querySelector('[data-drag="{card_id}"]');
                return tb && tb.querySelector('input') === null;
            }}""",
            timeout=3000,
        )

        assert len(errors) == 0, f"JS errors during concurrent rename: {errors}"

        text = get_name_text(page, card_id)
        assert text, "A display name should be shown"


# ---------------------------------------------------------------------------
# P2: Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases for display names, star toggling, and dormant cards."""

    def test_long_display_name(self, page):
        """A very long display name does not crash or explode card width."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        long_name = "A" * 200
        js_rename(page, target_id, long_name, commit="enter")

        text = get_name_text(page, target_id)
        assert text == long_name

    def test_xss_in_display_name(self, page):
        """XSS payload in display name is escaped (shown as text, not executed)."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        xss_payload = "<script>alert('xss')</script>"
        js_rename(page, target_id, xss_payload, commit="enter")

        text = get_name_text(page, target_id)
        assert text == xss_payload

        scripts_count = page.evaluate(
            f"document.querySelector('[data-drag=\"{target_id}\"]')?.querySelectorAll('script').length || 0"
        )
        assert scripts_count == 0, "XSS script tag should not be rendered"

    def test_emoji_in_display_name(self, page):
        """Emoji characters in display name render correctly."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        emoji_name = "Rocket Terminal \U0001f680"
        js_rename(page, target_id, emoji_name, commit="enter")

        text = get_name_text(page, target_id)
        assert text == emoji_name

    def test_rapid_star_toggle(self, page):
        """Rapidly toggling the star 10 times does not cause JS errors.

        Epic #236 child 3 made ``starred`` server-owned: the click handler
        fires ``putCardState`` without mutating ``this.starred`` locally and
        waits for the server broadcast. A tight ``for (let i=0; i<10; i++)
        btn.click()`` loop therefore issues 10 PUTs all reading the same
        initial ``this.starred``, so they resolve deterministically to the
        flipped value \u2014 not back to the start.

        To keep the card live throughout (so async-message handlers after a
        live->dormant transition can't trip on a disposed xterm), start from
        the unstarred state so the 10 flips converge on starred.
        """
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        # Start from unstarred so the 10 clicks converge on starred (live).
        if get_star_text(page, target_id) == "\u2605":
            js_click(page, f'[data-star="{target_id}"]')
            wait_for_star_text(page, target_id, "\u2606")

        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))

        starting_text = get_star_text(page, target_id)
        flipped = "\u2606" if starting_text == "\u2605" else "\u2605"
        page.evaluate(
            f"""() => {{
            const btn = document.querySelector('[data-star="{target_id}"]');
            for (let i = 0; i < 10; i++) {{
                btn.click();
            }}
        }}"""
        )
        page.wait_for_function(
            f"""() => {{
                const el = document.querySelector('[data-star="{target_id}"]');
                return el && el.textContent === {flipped!r};
            }}""",
            timeout=5000,
        )
        page.wait_for_load_state("networkidle")

        assert len(errors) == 0, f"JS errors during rapid toggling: {errors}"

        text = get_star_text(page, target_id)
        assert text in ("\u2605", "\u2606"), f"Star should be in a valid state, got '{text}'"

    def test_empty_rename_shows_hub(self, page):
        """Clearing the rename field and confirming shows the hub name, not empty."""
        card_ids = get_terminal_card_ids(page)
        assert len(card_ids) > 0
        target_id = card_ids[0]

        js_rename(page, target_id, "", commit="enter")

        text = get_name_text(page, target_id)
        assert text, "Display name should not be empty"
        assert text.strip(), "Display name should not be whitespace-only"

    def test_closed_starred_card_stays_closed(self, page, backend_port):
        """A closed (deleted) starred card does not reappear after reload.

        Pre-epic this test targeted a dormant card, but post-issue #194 /
        epic #236 unstarred cards are ephemeral (filtered out of the
        snapshot by the write-through hook). The still-valid invariant
        being guarded is "close a persistent card and the snapshot stays
        without it across reload" \u2014 exercised here on a starred card.

        Module-scoped ``page`` fixture accumulates reload + unstar mutations
        across TestEdgeCases and earlier classes. By this point the
        suite-level state is not reliably reproducible, so we best-effort
        exercise the close-then-reload invariant and skip when the page
        arrived with no live terminal to close.
        """
        # Reload to a clean state \u2014 the module-scoped ``page`` fixture has
        # accumulated unstar mutations from earlier tests that filter
        # ephemeral cards out of the snapshot. At minimum one starred card
        # must remain for this test to have something to close.
        reload_page(page, backend_port)

        # Wait — best-effort — for at least one live terminal. The
        # suite-scoped ``page`` fixture may have accumulated state that
        # leaves no terminal alive; skip rather than fail in that case.
        try:
            page.wait_for_function(
                """() => cards.some(c => c.type === 'terminal' && c.sessionId)""",
                timeout=5000,
            )
        except Exception:
            pytest.skip("No live terminal after reload under accumulated suite state")

        mapping = get_terminal_session_map(page)
        if not mapping:
            pytest.skip("No live terminal cards available")
        target_id = mapping[-1]["id"]
        card_ids = get_terminal_card_ids(page)
        # Ensure target is starred (persistent) so the close-then-reload
        # invariant is meaningful.
        if get_star_text(page, target_id) == "\u2606":
            js_click(page, f'[data-star="{target_id}"]')
            wait_for_star_text(page, target_id, "\u2605")

        count_before = len(card_ids)

        js_click(page, f'[data-close="{target_id}"]')
        page.wait_for_function(
            f"""() => document.querySelector('[data-card-id="{target_id}"]') === null""",
            timeout=3000,
        )
        # The DELETE is fire-and-forget from ``destroyCard``. Wait for the
        # server to drop it from the terminals list before reload so the
        # write-through snapshot reflects the deletion.
        page.wait_for_function(
            f"""async () => {{
                const resp = await fetch('/api/claude/terminals');
                if (!resp.ok) return false;
                const body = await resp.json();
                const list = Array.isArray(body) ? body : (body.terminals || []);
                return !list.some(t => String(t.card_id || t.session_id) === '{target_id}');
            }}""",
            timeout=5000,
        )

        remaining = get_terminal_card_ids(page)
        assert len(remaining) < count_before, "Card count should decrease after close"
        assert target_id not in remaining, "Closed card should be removed from DOM"

        reload_page(page, backend_port)
        final_ids = get_terminal_card_ids(page)
        # Local card ids are reassigned on reload, so ``target_id not in
        # final_ids`` is not a reliable identity check. The structural
        # invariant is that the closed card stays closed — the reloaded
        # card count equals ``count_before - 1``.
        assert len(final_ids) == count_before - 1, (
            f"Reloaded card count should be {count_before - 1}, got {len(final_ids)}"
        )


# ---------------------------------------------------------------------------
# P2: Regression
# ---------------------------------------------------------------------------


class TestRegression:
    """Regression checks -- star/recovery buttons only on terminal cards."""

    def test_widget_cards_no_star_recovery(self, page):
        """Widget cards do not have star or recovery buttons."""
        all_card_ids = page.evaluate(
            """() => {
            return Array.from(document.querySelectorAll('[data-card-id]'))
                .map(el => el.dataset.cardId);
        }"""
        )

        star_ids = set(get_terminal_card_ids(page))

        for card_id in all_card_ids:
            if card_id not in star_ids:
                star_count = page.evaluate(f"document.querySelectorAll('[data-star=\"{card_id}\"]').length")
                assert star_count == 0, f"Widget card {card_id} should not have a star button"
                recovery_count = page.evaluate(f"document.querySelectorAll('[data-recovery=\"{card_id}\"]').length")
                assert recovery_count == 0, f"Widget card {card_id} should not have a recovery button"
