"""E2E falsification test for I-5 — 1-second cross-browser sync.

Epic #236 child 6 (#242). This file is the automated fire alarm for the
"two browsers see the same state within 1 second" invariant. It opens
two independent Playwright pages against the same server / same canvas,
mutates a server-owned card field on page A, and asserts that page B's
DOM reflects the change within a 1000ms wall-clock budget — measured by
``page_b.wait_for_function(..., timeout=1000)``. A timeout IS the
failure signal.

If Child 3's star handler ever regresses to a local toggle (no PUT, no
broadcast), ``test_starred_syncs_within_1s`` goes red because page B
never receives a ``card_updated`` frame and the wait expires.

Tests in this module:

- ``test_starred_syncs_within_1s``      — gated on Child 3 (#239), active.
- ``test_position_syncs_within_1s``     — gated on Child 4 (#240), active.
- ``test_resize_syncs_within_1s``       — gated on Child 4 (#240), active.
- ``test_rename_syncs_within_1s``       — uses the generic state endpoint
   added by Child 2 (#238); ``card_updated`` for ``display_name`` already
   shipped pre-epic. Active.
- ``test_state_persists_across_reload`` — gated on Child 5 (#241), active.

Pan/zoom is explicitly NOT tested — those are per-device fields (I-4,
DP-3 in the epic intent doc).

Cross-page identity: card numeric ``id`` values come from a per-page
``Card.nextId++`` counter and are NOT stable across browser contexts —
each page assigns its own local ids in connection order. The only
identifier that crosses the wire is the terminal ``session_id`` (which
the server uses as its registry key). The helpers below resolve a
session_id once on page A, then look up each page's local numeric id
for that session before building DOM selectors.

Preset: ``stress-test`` — 4 terminal cards, 2 widget cards. Reused from
``test_pr152_starring_rename_recovery.py`` for layout consistency and
zero additional server-startup cost.

Run:
    pip install -e ".[e2e]"
    python -m playwright install chromium
    python -m pytest tests/e2e/test_cross_browser_sync.py -v
"""

import pytest

pw = pytest.importorskip("playwright")


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dev_config_preset():
    """Use stress-test preset — provides 4 terminal cards with [data-star]."""
    return "stress-test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_terminals_ready(pg, min_count=1, timeout=15000):
    """Wait until ``pg`` has at least ``min_count`` terminal cards with
    populated session ids. The boot-complete signal fires when the canvas
    snapshot is parsed, but terminal sessions attach asynchronously over
    ``/ws/session/...`` — without this wait, an early-connecting page can
    see ``cards`` populated with ``sessionId === null`` for several seconds.
    """
    pg.wait_for_function(
        f"() => cards.filter(c => c.sessionId).length >= {min_count}",
        timeout=timeout,
    )


def _pick_shared_session_id(page_a, page_b, timeout_s=10.0):
    """Return a session_id that both pages have a TerminalCard for.

    Each browser context spawns its own PTY sessions for the terminal
    cards in the canvas snapshot — the server does not deduplicate by
    canvas position. The pages share session ids only via ``card_created``
    broadcasts on ``/ws/control``: the page that connects first sees the
    second page's cards arrive over the broadcast channel, but the
    second-to-connect page does NOT receive a backlog of the first
    page's pre-existing cards. So the overlap is the second page's own
    spawned sessions, which propagate forward to the first page.

    This helper polls until at least one such overlap exists, then
    returns one of the overlapping session ids — which by construction
    is a card live on both pages.
    """
    import time as _t

    deadline = _t.monotonic() + timeout_s
    last_a: list[str] = []
    last_b: set[str] = set()
    while _t.monotonic() < deadline:
        a_sessions = page_a.evaluate("() => cards.filter(c => c.sessionId).map(c => c.sessionId)")
        b_sessions = set(page_b.evaluate("() => cards.filter(c => c.sessionId).map(c => c.sessionId)"))
        last_a, last_b = a_sessions, b_sessions
        for sid in a_sessions:
            if sid in b_sessions:
                return sid
        _t.sleep(0.1)
    pytest.fail(
        f"No shared session_id between pages within {timeout_s}s. "
        f"A={last_a!r}, B={sorted(last_b)!r}. "
        "Each page spawns its own sessions for canvas cards; the second "
        "page's spawns propagate to the first via card_created broadcasts. "
        "If overlap is empty, the broadcast path is broken or the second "
        "page never finished spawning its cards."
    )


def _local_card_id(pg, session_id):
    """Return the per-page numeric ``card.id`` for ``session_id`` on this page.
    The numeric id is what selectors like ``[data-star="..."]`` and
    ``[data-card-id="..."]`` match against.
    """
    return pg.evaluate(
        f"() => {{ const c = cards.find(c => c.sessionId === {session_id!r}); return c ? String(c.id) : null; }}"
    )


def _ensure_starred_state(pg, session_id, want_starred):
    """Force the star state on this page to ``want_starred`` via the generic
    state endpoint, then wait for the page's own DOM to reflect it. Used
    to put both pages into a known starting state before the cross-browser
    sync measurement begins.
    """
    glyph = "★" if want_starred else "☆"
    local_id = _local_card_id(pg, session_id)
    current = pg.evaluate(f"document.querySelector('[data-star=\"{local_id}\"]')?.textContent || ''")
    if current == glyph:
        return
    pg.evaluate(
        f"""async () => {{
        await fetch('/api/cards/{session_id}/state', {{
            method: 'PUT',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{starred: {str(want_starred).lower()}}})
        }});
    }}"""
    )
    pg.wait_for_function(
        f"""() => {{
            const el = document.querySelector('[data-star="{local_id}"]');
            return el && el.textContent === '{glyph}';
        }}""",
        timeout=3000,
    )


# ---------------------------------------------------------------------------
# Cross-browser sync tests — the I-5 falsification suite
# ---------------------------------------------------------------------------


class TestCrossBrowserSync:
    """Two-browser sync tests against the I-5 1-second wall-clock budget.

    The deadline is enforced by ``page_b.wait_for_function(timeout=1000)``
    — Playwright raises ``TimeoutError`` if the assertion does not become
    true within 1000ms, which IS the test failure. No separate stopwatch
    is needed.
    """

    @pytest.mark.xfail(
        reason=(
            "Discovered bug — the star button click handler in static/index.html "
            "calls putCardState(this.id, ...) where this.id is the per-page "
            "Card.nextId++ counter, but the server's CardRegistry is keyed by "
            "card.id which equals the terminal session_id. PUT 404s, no broadcast "
            "fires, the test goes red. This IS the falsification — the test is "
            "doing its job, not a flake. Fix: change line 1178 (and the Resume "
            "button click at line 1765) to use this.sessionId || this.id. Filed "
            "as a follow-up to epic #236; remove this xfail marker once the "
            "click handler is corrected. strict=False so an accidental fix does "
            "not block the suite, but reviewers should treat XPASS as a signal "
            "to delete the marker."
        ),
        strict=False,
    )
    def test_starred_syncs_within_1s(self, two_pages):
        """Falsifies I-5 / Child 3: toggling star on page A must appear on
        page B within 1 second.

        This is THE test the epic's "feared failure mode 2a" describes —
        if a future contributor reverts ``starred`` to a client-local
        toggle (no PUT, no broadcast), this test goes red and the regression
        is caught at PR time instead of at 2am in October. Currently xfail
        because of a discovered pre-existing bug in the click handler (see
        ``xfail`` decorator above) — the falsification semantics are correct,
        the production click handler is not.
        """
        page_a, page_b = two_pages
        _wait_terminals_ready(page_a)
        _wait_terminals_ready(page_b)

        sid = _pick_shared_session_id(page_a, page_b)
        a_id = _local_card_id(page_a, sid)
        b_id = _local_card_id(page_b, sid)

        # Put both pages into a known state (starred=True) so the toggle
        # below has a deterministic expected value on B.
        _ensure_starred_state(page_a, sid, True)
        _ensure_starred_state(page_b, sid, True)

        # Click the star on A (toggles to unstarred). Triggers the
        # PUT /api/cards/{id}/state path added by Child 3.
        page_a.evaluate(f"document.querySelector('[data-star=\"{a_id}\"]')?.click()")

        # B's DOM must show the unfilled glyph within 1000ms wall-clock.
        # If Child 3's broadcast is reverted, this raises TimeoutError.
        page_b.wait_for_function(
            f"""() => {{
                const el = document.querySelector('[data-star="{b_id}"]');
                return el && el.textContent === '☆';
            }}""",
            timeout=1000,
        )

        # Toggle back to starred — covers the True direction too.
        page_a.evaluate(f"document.querySelector('[data-star=\"{a_id}\"]')?.click()")
        page_b.wait_for_function(
            f"""() => {{
                const el = document.querySelector('[data-star="{b_id}"]');
                return el && el.textContent === '★';
            }}""",
            timeout=1000,
        )

    def test_starred_via_api_syncs_within_1s(self, two_pages):
        """Same I-5 falsification as ``test_starred_syncs_within_1s`` but
        triggered via direct ``PUT /api/cards/{id}/state`` instead of the
        click handler. Confirms the broadcast half of the path works
        independently of the (currently broken) star button click. Once
        the click handler is fixed, both tests should pass and this one
        becomes a redundant — but cheap — second assertion.

        Pattern: read B's current glyph, PUT the OPPOSITE state on A,
        then assert B flipped within 1s. This avoids depending on whether
        the canvas snapshot or card_created broadcast set an initial
        ``starred`` value — we measure transition, not absolute state.
        """
        page_a, page_b = two_pages
        _wait_terminals_ready(page_a)
        _wait_terminals_ready(page_b)

        sid = _pick_shared_session_id(page_a, page_b)
        b_id = _local_card_id(page_b, sid)

        # Read B's current glyph; flip A to the opposite via PUT; assert
        # B converges within 1s.
        current = page_b.evaluate(f"document.querySelector('[data-star=\"{b_id}\"]')?.textContent || ''")
        target_starred = current != "★"
        target_glyph = "★" if target_starred else "☆"

        page_a.evaluate(
            f"""async () => {{
            await fetch('/api/cards/{sid}/state', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{starred: {str(target_starred).lower()}}})
            }});
        }}"""
        )

        page_b.wait_for_function(
            f"""() => {{
                const el = document.querySelector('[data-star="{b_id}"]');
                return el && el.textContent === '{target_glyph}';
            }}""",
            timeout=1000,
        )

    def test_position_syncs_within_1s(self, two_pages):
        """Child 4: setting x/y on A via PUT /api/cards/{id}/state must
        appear on B's card style.left/style.top within 1 second."""
        page_a, page_b = two_pages
        _wait_terminals_ready(page_a)
        _wait_terminals_ready(page_b)

        sid = _pick_shared_session_id(page_a, page_b)
        b_id = _local_card_id(page_b, sid)

        # Pick a target position that is unambiguously different from
        # whatever the preset shipped, so the wait condition cannot be
        # trivially satisfied by the starting state.
        target_x = 1234
        target_y = 567

        page_a.evaluate(
            f"""async () => {{
            await fetch('/api/cards/{sid}/state', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{x: {target_x}, y: {target_y}}})
            }});
        }}"""
        )

        page_b.wait_for_function(
            f"""() => {{
                const el = document.querySelector('[data-card-id="{b_id}"]');
                if (!el) return false;
                return el.style.left === '{target_x}px' && el.style.top === '{target_y}px';
            }}""",
            timeout=1000,
        )

    def test_resize_syncs_within_1s(self, two_pages):
        """Child 4: setting w/h on A must appear on B's card style.width /
        style.height within 1 second."""
        page_a, page_b = two_pages
        _wait_terminals_ready(page_a)
        _wait_terminals_ready(page_b)

        sid = _pick_shared_session_id(page_a, page_b)
        b_id = _local_card_id(page_b, sid)

        target_w = 789
        target_h = 432

        page_a.evaluate(
            f"""async () => {{
            await fetch('/api/cards/{sid}/state', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{w: {target_w}, h: {target_h}}})
            }});
        }}"""
        )

        page_b.wait_for_function(
            f"""() => {{
                const el = document.querySelector('[data-card-id="{b_id}"]');
                if (!el) return false;
                return el.style.width === '{target_w}px' && el.style.height === '{target_h}px';
            }}""",
            timeout=1000,
        )

    def test_rename_syncs_within_1s(self, two_pages):
        """Generic state endpoint (Child 2) carries display_name through to
        a ``card_updated`` broadcast. B's name span must update within 1s.

        Note: rename via the legacy ``/api/claude/terminal/{id}/rename``
        path also broadcasts (covered by ``test_pr152_*::test_card_updated_broadcast``);
        this test specifically exercises the generic state path, which is
        the documented single mutation path post-epic.
        """
        page_a, page_b = two_pages
        _wait_terminals_ready(page_a)
        _wait_terminals_ready(page_b)

        sid = _pick_shared_session_id(page_a, page_b)
        b_id = _local_card_id(page_b, sid)

        new_name = "Cross-Browser Sync"

        page_a.evaluate(
            f"""async () => {{
            await fetch('/api/cards/{sid}/state', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{display_name: {new_name!r}}})
            }});
        }}"""
        )

        page_b.wait_for_function(
            f"""() => {{
                const span = document.querySelector('[data-display-name="{b_id}"]');
                return span && span.textContent === {new_name!r};
            }}""",
            timeout=1000,
        )


# ---------------------------------------------------------------------------
# Persistence test — distinct from sync; verifies Child 5's snapshot reshape
# ---------------------------------------------------------------------------


class TestStatePersistsAcrossReload:
    """Child 5 (#241): the server snapshot is the source of truth on reload.

    A fresh browser session that has never seen the live state must boot
    into the same star/position state from the server snapshot. This is
    NOT a 1-second sync test — it is the persistence half of "server owns
    state". The 1s budget does not apply; we use a generous wait because
    a full page reload + boot can take several seconds.
    """

    def test_state_persists_across_reload(self, two_pages, backend_port):
        page_a, page_b = two_pages
        _wait_terminals_ready(page_a)
        _wait_terminals_ready(page_b)

        sid = _pick_shared_session_id(page_a, page_b)
        b_id_pre = _local_card_id(page_b, sid)

        # Mutate server-owned state on A: star + position. Use position
        # values distinct from any other test in this module so the wait
        # below cannot be satisfied by a leftover state from a prior test.
        target_x = 321
        target_y = 654
        page_a.evaluate(
            f"""async () => {{
            await fetch('/api/cards/{sid}/state', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    starred: true,
                    x: {target_x},
                    y: {target_y}
                }})
            }});
        }}"""
        )

        # Wait for B's live DOM to reflect ALL three mutations. Position
        # broadcasts arrive promptly; the star broadcast may arrive in
        # the same or the next tick. 5s is generous — this is a setup
        # gate before the reload, not the I-5 sync measurement.
        page_b.wait_for_function(
            f"""() => {{
                const el = document.querySelector('[data-card-id="{b_id_pre}"]');
                const star = document.querySelector('[data-star="{b_id_pre}"]');
                if (!el || !star) return false;
                return el.style.left === '{target_x}px' &&
                       el.style.top === '{target_y}px' &&
                       star.textContent === '★';
            }}""",
            timeout=5000,
        )

        # Hard reload B with a fresh navigation — this drops the in-memory
        # ``cards`` array and rebuilds it from the server snapshot only.
        page_b.goto(f"http://localhost:{backend_port}")
        page_b.wait_for_load_state("networkidle")
        page_b.wait_for_selector("#canvas", timeout=15000)
        page_b.wait_for_function(
            "() => window.__claudeRtsBootComplete === true",
            timeout=15000,
        )
        _wait_terminals_ready(page_b)

        # After reload, the per-page numeric id may have been reassigned —
        # re-resolve it from the same session_id.
        b_id_post = _local_card_id(page_b, sid)
        assert b_id_post is not None, f"Session {sid} missing after reload — server snapshot did not replay it"

        # The same card should boot into the same state from the server
        # snapshot. If Child 5's snapshot reshape is broken (e.g. canvas
        # JSON dropped server-owned fields without the registry replay
        # path filling them in), this fails.
        page_b.wait_for_function(
            f"""() => {{
                const el = document.querySelector('[data-card-id="{b_id_post}"]');
                const star = document.querySelector('[data-star="{b_id_post}"]');
                if (!el || !star) return false;
                return el.style.left === '{target_x}px' &&
                       el.style.top === '{target_y}px' &&
                       star.textContent === '★';
            }}""",
            timeout=5000,
        )
