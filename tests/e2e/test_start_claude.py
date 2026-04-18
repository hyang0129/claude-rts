"""Playwright e2e tests for the Start Claude button on terminal cards.

Uses the ``start-claude`` dev-config preset which has:
- main_profile_name set to "main" (default)
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
    return page.evaluate(
        """(cardId) => {
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
    """The Start Claude button on terminal cards uses the main profile slot (#163)."""

    def test_claude_btn_visible_on_terminal_card(self, page):
        """Terminal cards in the start-claude preset have a .claude-btn."""
        card, btn = _find_terminal_card(page)
        assert btn is not None, "Expected at least one .claude-btn on a terminal card"
        assert btn.is_visible()

    def test_main_profile_api_returns_main(self, page, backend_port):
        """The /api/profiles/main endpoint returns the configured main slot name."""
        result = page.evaluate(
            """async (port) => {
                const resp = await fetch(`http://localhost:${port}/api/profiles/main`);
                return await resp.json();
            }""",
            backend_port,
        )
        assert result.get("main_profile_name") == "main", f"Expected main_profile_name='main', got {result}"
        # exists may be True or False depending on whether creds were copied in —
        # we only assert the field is present as a boolean.
        assert isinstance(result.get("exists"), bool)

    def test_no_main_credentials_shows_warning(self, page):
        """When the main slot has no credentials, clicking Start Claude shows a warning.

        The start-claude preset doesn't pre-populate /profiles/main/.credentials.json,
        so the button should surface the 'no credentials yet' hint instead of sending
        the env command.
        """
        card, btn = _find_terminal_card(page)
        if btn is None:
            pytest.skip("No terminal card with .claude-btn found")

        btn.click()

        # The Start Claude click handler writes one of two known warning strings
        # synchronously when the main slot is not ready:
        card_id = card.get_attribute("data-card-id")
        page.wait_for_function(
            """(cardId) => {
                const c = cards.find(c => String(c.id) === String(cardId));
                if (!c || !c.term) return false;
                const buf = c.term.buffer.active;
                for (let i = 0; i < buf.length; i++) {
                    const line = buf.getLine(i);
                    if (!line) continue;
                    const text = line.translateToString(true);
                    if (text.includes('No main profile configured') ||
                        text.includes('Main profile has no credentials yet')) {
                        return true;
                    }
                }
                return false;
            }""",
            arg=card_id,
            timeout=10000,
        )

        content = _get_xterm_content(page, card)
        assert "No main profile configured" in content or "Main profile has no credentials yet" in content, (
            f"Expected main-profile warning in xterm buffer.\nGot: {content[:500]}"
        )
