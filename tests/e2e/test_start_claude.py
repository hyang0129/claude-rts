"""Playwright e2e tests for the Start Claude button on terminal cards.

Uses the ``start-claude`` dev-config preset which has:
- priority_profile set to "test-profile"
- A Profile Manager widget and a terminal card pre-placed on canvas

Run:
    HEADED=1 python -m pytest tests/e2e/test_start_claude.py -v
"""

import pytest

pw = pytest.importorskip("playwright")


# ── Override the preset to start-claude ────────────────────────────────────────


@pytest.fixture(scope="module")
def dev_config_preset():
    return "start-claude"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _find_terminal_card(page):
    """Return the first card element that has a .claude-btn (i.e. a terminal card)."""
    btn = page.locator(".claude-btn").first
    if btn.count() == 0:
        return None, None
    card = btn.locator("xpath=ancestor::*[@data-card-id]")
    if card.count() == 0:
        return None, None
    return card, btn


def _get_card_session_id(page, card):
    """Get the WebSocket session ID from the frontend card object."""
    card_id = card.get_attribute("data-card-id")
    # `cards` is a module-level let in index.html, accessible from page context
    return page.evaluate(
        """(cardId) => {
            // cards is a let in module scope — accessible via eval in page context
            const c = cards.find(c => String(c.id) === String(cardId));
            return c ? c.sessionId : null;
        }""",
        card_id,
    )


def _read_session_scrollback(page, backend_port, session_id):
    """Read scrollback for a specific session via the puppeting API."""
    return page.evaluate(
        """async ([port, sid]) => {
            const resp = await fetch(
                `http://localhost:${port}/api/test/session/${sid}/read`
            );
            if (!resp.ok) return { error: `HTTP ${resp.status}` };
            return await resp.json();
        }""",
        [backend_port, session_id],
    )


def _get_xterm_content(page, card):
    """Read the visible text content from the xterm.js terminal in this card."""
    card_id = card.get_attribute("data-card-id")
    return page.evaluate(
        """(cardId) => {
            const c = cards.find(c => String(c.id) === String(cardId));
            if (!c || !c.term) return '';
            const buf = c.term.buffer.active;
            let lines = [];
            for (let i = 0; i < buf.length; i++) {
                const line = buf.getLine(i);
                if (line) lines.push(line.translateToString(true));
            }
            return lines.join('\\n');
        }""",
        card_id,
    )


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestStartClaudeButton:
    """The Start Claude button on terminal cards uses the priority profile."""

    def test_claude_btn_visible_on_terminal_card(self, page):
        """Terminal cards in the start-claude preset have a .claude-btn."""
        card, btn = _find_terminal_card(page)
        assert btn is not None, "Expected at least one .claude-btn on a terminal card"
        assert btn.is_visible()

    def test_claude_btn_sends_priority_profile_command(self, page, backend_port):
        """Clicking Start Claude sends 'env CLAUDE_CONFIG_DIR=/profiles/<name> claude'.

        The start-claude preset sets priority_profile to "test-profile".
        We verify the command appears in the server-side PTY scrollback after clicking.

        We read from the server-side scrollback (not the xterm.js viewport) because
        Claude's startup output can scroll the command echo out of the visible buffer
        before the test reads it.
        """
        card, btn = _find_terminal_card(page)
        if btn is None:
            pytest.skip("No terminal card with .claude-btn found")

        # Wait for the WebSocket session to be established by polling the
        # frontend card's sessionId rather than sleeping a fixed 3 s.
        card_id = card.get_attribute("data-card-id")
        page.wait_for_function(
            """(cardId) => {
                const c = cards.find(c => String(c.id) === String(cardId));
                return c && typeof c.sessionId === 'string' && c.sessionId.length > 0;
            }""",
            arg=card_id,
            timeout=15000,
        )

        session_id = _get_card_session_id(page, card)
        if not session_id:
            pytest.skip("Could not obtain session ID for terminal card")

        btn.click()

        # Poll the server-side scrollback for the expected fragment rather
        # than sleeping a fixed 1 s and hoping the PTY echoed in time.  The
        # puppeting API returns {"output": "<raw text>", ...}.
        expected_fragment = "CLAUDE_CONFIG_DIR=/profiles/test-profile"
        page.wait_for_function(
            """async ([port, sid, fragment]) => {
                const resp = await fetch(
                    `http://localhost:${port}/api/test/session/${sid}/read`
                );
                if (!resp.ok) return false;
                const data = await resp.json();
                return typeof data.output === 'string' && data.output.includes(fragment);
            }""",
            arg=[backend_port, session_id, expected_fragment],
            timeout=10000,
        )

        # Read server-side scrollback — this survives Claude's startup output
        # scrolling the viewport, since the ring buffer holds 64KB of raw PTY data.
        # The API returns {"output": "<raw text>", "size": N, "total_written": N}.
        result = _read_session_scrollback(page, backend_port, session_id)
        scrollback = result.get("output", "") if isinstance(result, dict) else ""

        assert expected_fragment in scrollback, (
            f"Expected '{expected_fragment}' in server scrollback.\nGot: {scrollback[:500]}"
        )

    def test_priority_profile_api_returns_test_profile(self, page, backend_port):
        """The /api/profiles/priority endpoint returns the preset's priority_profile."""
        result = page.evaluate(
            """async (port) => {
                const resp = await fetch(`http://localhost:${port}/api/profiles/priority`);
                return await resp.json();
            }""",
            backend_port,
        )
        assert result.get("priority_profile") == "test-profile", (
            f"Expected priority_profile='test-profile', got {result}"
        )

    def test_no_priority_shows_warning(self, page, backend_port):
        """When priority_profile is cleared, clicking Start Claude shows a warning."""
        card, btn = _find_terminal_card(page)
        if btn is None:
            pytest.skip("No terminal card with .claude-btn found")

        # Clear the priority profile and wait for the PUT to land rather
        # than sleeping 500 ms afterwards.
        with page.expect_response(
            lambda r: "/api/profiles/priority" in r.url and r.request.method == "PUT",
            timeout=10000,
        ):
            page.evaluate(
                """async (port) => {
                    await fetch(`http://localhost:${port}/api/profiles/priority`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ priority_profile: null }),
                    });
                }""",
                backend_port,
            )

        btn.click()

        # Poll the xterm buffer for the warning rather than sleeping 1 s.
        # The Start Claude click-handler writes this string synchronously
        # when priority_profile is unset, so the buffer flips on the next
        # xterm render frame.
        card_id = card.get_attribute("data-card-id")
        page.wait_for_function(
            """(cardId) => {
                const c = cards.find(c => String(c.id) === String(cardId));
                if (!c || !c.term) return false;
                const buf = c.term.buffer.active;
                for (let i = 0; i < buf.length; i++) {
                    const line = buf.getLine(i);
                    if (line && line.translateToString(true).includes('No priority profile set')) {
                        return true;
                    }
                }
                return false;
            }""",
            arg=card_id,
            timeout=10000,
        )

        # Warning is written to xterm.js locally (not to server scrollback)
        content = _get_xterm_content(page, card)
        assert "No priority profile set" in content, (
            f"Expected warning about no priority profile in xterm buffer.\nGot: {content[:500]}"
        )

        # Restore priority profile for subsequent tests
        page.evaluate(
            """async (port) => {
                await fetch(`http://localhost:${port}/api/profiles/priority`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ priority_profile: 'test-profile' }),
                });
            }""",
            backend_port,
        )
