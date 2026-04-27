"""Synthetic Phase 1 QA scenario — Container Manager + terminal card.

Debt issue: #221 (PR #220 — Container Manager epic)
Scenario:   Verify the Container Manager widget renders and a terminal card
            can be spawned through the Claude API, then appears on the canvas.

This scenario targets the following item from the #221 debt backlog:
  "Start server with --dev-config default, spawn a Container Manager widget —
   verify it shows 'Container Manager' (not VM Manager)"

It also exercises the terminal creation API so a real card appears on canvas
and accepts keyboard input, satisfying the Phase 1 end-to-end wiring test.

Real-app pathway:
  1. Start server with --dev-config default (no test endpoints used).
  2. Open canvas in Playwright.
  3. Right-click to open context menu — spawn a Container Manager widget.
  4. Verify the widget body DOM is loaded (widget title area rendered).
  5. Use the REST API (real production endpoint, not /api/test/...) to
     create a terminal card: POST /api/claude/terminal/create.
  6. Wait for the terminal card to appear on the canvas DOM.
  7. Present human gate: "Does the Container Manager widget say
     'Container Manager' and does the terminal card appear?"
"""

from __future__ import annotations


from claude_rts.qa_scenario import HumanGate, QAScenario


class Scenario(QAScenario):
    scenario_id = "syn-001-container-terminal"
    debt_issue = 221
    preset = "default"

    def run_setup(self, page) -> HumanGate:
        """Drive the app to the gate state.

        Steps:
        1. Confirm canvas is loaded.
        2. Right-click the viewport and spawn a Container Manager widget.
        3. Create a terminal card via POST /api/claude/terminal/create (real endpoint).
        4. Wait for the terminal card DOM element to appear.
        """
        # Step 1: Confirm canvas is ready (boot complete signal already waited
        # by the runner before calling run_setup).
        page.wait_for_selector("#canvas", timeout=15000)

        # Step 2: Spawn a Container Manager widget via right-click context menu.
        # The context menu is opened at the center of the viewport.
        page.evaluate("() => { if (typeof hideContextMenu === 'function') hideContextMenu(); }")
        page.locator("#viewport").click(button="right", position={"x": 500, "y": 400})

        # Wait for context menu to be visible.
        page.wait_for_selector("#context-menu.visible", timeout=5000)

        # Click the "Container Manager" widget item. The label is rendered as a
        # .ctx-item element with data-widget="container-manager".
        cm_item = page.locator("#context-menu .ctx-item[data-widget='container-manager']")
        cm_item.wait_for(state="visible", timeout=5000)
        cm_item.click()

        # Close any residual context menu.
        page.evaluate("() => { if (typeof hideContextMenu === 'function') hideContextMenu(); }")

        # Wait for the Container Manager card body to render (widget loads asynchronously).
        # The Container Manager card injects a [data-container-search] input when rendered.
        page.wait_for_selector("[data-container-search]", timeout=10000)

        # Step 3: Create a terminal card via the real production Claude API.
        # This endpoint is the standard terminal creation path used by Canvas Claude.
        response = page.evaluate(
            """async () => {
                const r = await fetch('/api/claude/terminal/create', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({cmd: 'bash', x: 200, y: 200, w: 600, h: 400})
                });
                if (!r.ok) return {ok: false, status: r.status};
                const data = await r.json();
                return {ok: true, card_id: data.card_id};
            }"""
        )

        if not response.get("ok"):
            # Non-fatal: the terminal spawn may fail if bash is not available in
            # the dev-config server process (e.g. test env).  The Container Manager
            # widget is still the primary judgment target.
            pass
        else:
            card_id = response.get("card_id")
            if card_id:
                # Step 4: Wait for the terminal card DOM element to appear.
                page.wait_for_selector(f'[data-card-id="{card_id}"]', timeout=10000)

        # Gate: the human judges both the Container Manager label and the
        # terminal card presence.
        return HumanGate(
            scenario_id=self.scenario_id,
            question=(
                "1. Does the Container Manager widget title say 'Container Manager' "
                "(not 'VM Manager' or any other label)?\n"
                "   2. Does a terminal card appear on the canvas?"
            ),
            expected=(
                "Widget title reads 'Container Manager'. "
                "A terminal card is visible on the canvas and accepts keyboard input."
            ),
        )
