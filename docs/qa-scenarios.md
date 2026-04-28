# QA Scenarios — Runnable Discharge Path for QA Debt

This document describes the scenario format, CLI commands, verdict comment format,
and the Claude agent workflow for iterating through unverified scenarios.

See issue #277 for the design rationale.

---

## Overview

`/e2e-qa` records residual human-QA items as markdown checkboxes on a debt issue
(e.g. #221).  The *discharge path* converts those items into runnable scenarios:
a coding agent authors a Playwright Python file, Claude runs `qa run <id>` to
drive the app to the gate state and capture a screenshot, then records the human's
verdict with `qa verdict <id> <verdict>`.

**The GitHub issue is the only source of truth.** A scenario is verified when a
verdict comment exists on its linked debt issue. There is no local verdicts file.

**The CLI is a dumb runner.** `qa run` drives Playwright to the gate state and
takes a screenshot.  `qa verdict` posts the verdict comment.  Neither command
auto-selects scenarios or reads prior verdicts.

**A Claude agent is the orchestrator.** The agent reads the debt issue to find
unverified scenarios, calls `qa run <id>` for each, reads the screenshot, asks the
human what they saw, and calls `qa verdict <id> <verdict>`. See the
[Agent workflow](#agent-workflow) section.

```
qa_scenarios/<id>.py   — Playwright scenario file (authored by coding agent)
                │
                ▼
python -m claude_rts qa run <scenario-id>
                │
                ├─ starts app (--dev-config <preset>, port 3097)
                ├─ drives Playwright to gate state
                ├─ saves screenshot → /tmp/qa-screenshot-<id>.png
                ├─ prints gate question + expected
                └─ exits 0  (no verdict posted)

Claude reads screenshot, asks human what they saw
                │
                ▼
python -m claude_rts qa verdict <scenario-id> <verdict> [--notes "..."]
                │
                └─ posts verdict comment on debt_issue
```

---

## CLI reference

```bash
# List all available scenario IDs and their linked debt issues
python -m claude_rts qa list

# Drive the app to the gate state and capture a screenshot
python -m claude_rts qa run <scenario-id>

# Watch via noVNC while running (optional)
HEADED=1 python -m claude_rts qa run <scenario-id>

# Post a verdict after assessing the screenshot
python -m claude_rts qa verdict <scenario-id> <verdict> [--notes "..."]
```

Verdict options for `qa verdict`:

| Verdict | Meaning |
|---|---|
| `pass` | Gate state matches expected; feature works |
| `fail` | Gate state does not match; bug found |
| `inconclusive` | Cannot determine — screenshot unclear or ambiguous |
| `blocked` | Could not reach gate state (server error, missing dependency, etc.) |

Prerequisites:
```bash
pip install -e ".[e2e]"
python -m playwright install chromium
```

For headed mode, a display server is required. In the devcontainer, start it with:
```bash
bash .devcontainer/start-novnc.sh
# then open http://localhost:6081/vnc.html to view the browser window
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | `qa run`: screenshot captured successfully |
| 0 | `qa verdict`: verdict comment posted |
| 1 | Scenario not found, server failed to start, invalid verdict, or GitHub comment failed |

---

## Scenario file format

Every file in `qa_scenarios/` must define a class named `Scenario` that satisfies
the `QAScenario` protocol from `claude_rts.qa_scenario`.

```python
from claude_rts.qa_scenario import HumanGate, QAScenario

class Scenario(QAScenario):
    # Required class attributes
    scenario_id = "pr220-s1-container-label"   # unique identifier
    debt_issue  = 221                           # GitHub issue number of the debt backlog
    preset      = "default"                    # dev-config preset to start the server with

    def run_setup(self, page) -> HumanGate:
        """Drive the app to the visual state where human judgment is needed.

        ``page`` is a live Playwright ``Page`` already connected to the running app.
        Use ONLY real app pathways — no /api/test/... endpoints.
        """
        page.wait_for_selector("#canvas")
        # ... real UI interactions here ...
        return HumanGate(
            scenario_id=self.scenario_id,
            question="Does the Container Manager widget title say 'Container Manager'?",
            expected="Title text reads 'Container Manager', not 'VM Manager'",
        )
```

### Class attributes

| Attribute | Type | Required | Description |
|---|---|---|---|
| `scenario_id` | `str` | Yes | Unique identifier used in the verdict comment |
| `debt_issue` | `int` | Yes | GitHub issue number where the verdict comment is posted |
| `preset` | `str` | No | Dev-config preset name (default: `"default"`) |

### `run_setup(page) -> HumanGate`

- Called with a Playwright `Page` already navigated to the running app.
- Must use **real app pathways only** — no `/api/test/...` endpoints.
- Must return a `HumanGate(scenario_id, question, expected)`.
- A screenshot is taken immediately after `run_setup` returns.

---

## Naming convention

Files are sorted **alphabetically** by filename — naming encodes priority:

| Prefix | Meaning |
|---|---|
| `syn-NNN-` | Synthetic scenario (not tied to a specific PR) |
| `pr<N>-sN-` | Scenario for PR #N, item N in that PR's checklist |

Examples:
- `syn-001-container-terminal.py` — first synthetic scenario
- `pr220-s1-container-label.py` — PR #220, item 1

---

## Verdict comment format

Posted to the linked debt issue by `qa verdict`:

```markdown
## QA Verdict: PASS ✓

**Scenario:** `syn-001-container-terminal`
**Question:** Does the Container Manager widget say 'Container Manager'...
**Expected:** Widget title reads 'Container Manager'...
**Verdict:** pass
**Notes:** Label correct, terminal card visible on canvas
**Commit:** `abc1234`
**Timestamp:** 2026-04-28T16:00:00+00:00

*Recorded by `python -m claude_rts qa verdict syn-001-container-terminal pass`*
```

An agent can determine whether a scenario is verified by searching issue comments
for a block matching `## QA Verdict` + `**Scenario:** \`<id>\``.

### Verdict semantics

| Verdict | Label in comment | Meaning |
|---|---|---|
| `pass` | PASS ✓ | Gate state matches expected |
| `fail` | FAIL ✗ | Gate state does not match |
| `inconclusive` | INCONCLUSIVE ? | Cannot determine from screenshot |
| `blocked` | BLOCKED ⊘ | Could not reach gate state |

---

## Agent workflow

The following prompt instructs a Claude agent to iterate through all unverified
scenarios on a debt issue and run each one:

```
You are discharging QA debt on GitHub issue #<N> in the hyang0129/supreme-claudemander repo.

Steps:
1. Read the issue comments with: gh issue view <N> --repo hyang0129/supreme-claudemander --comments
2. Run: python -m claude_rts qa list
   This gives you all available scenario IDs.
3. For each scenario ID, check whether the issue comments already contain a block
   matching "## QA Verdict" and "**Scenario:** `<id>`".
   If a matching comment exists, the scenario is verified — skip it.
4. For each unverified scenario:
   a. Run: python -m claude_rts qa run <scenario-id>
      This drives the app to the gate state and saves a screenshot.
   b. Read the screenshot at the printed path with your image tool.
   c. Describe what you see to the human and ask whether it matches the expected state.
   d. Based on the human's response, run:
      python -m claude_rts qa verdict <scenario-id> <pass|fail|inconclusive|blocked> \
        --notes "<what the human said>"
5. After all scenarios are run, re-read the issue comments to confirm all verdict
   comments are present.
```

---

## Authoring a new scenario

1. Identify a debt item from the linked `qa-debt` issue.
2. Create `qa_scenarios/<prefix>-<NNN>-<slug>.py` with a `Scenario` class.
3. Implement `run_setup(page)` using real app pathways (no test endpoints).
4. Smoke-check: run `python -m claude_rts qa run <id>` to verify
   Playwright reaches the gate state without error and a screenshot is saved.
5. Commit the scenario file.

---

## What NOT to do

- Do NOT call `/api/test/...` endpoints in `run_setup()`.
- Do NOT rely on local files for verdict state — read the GitHub issue comments.
- Do NOT write `qa_scenarios/__init__.py` — the runner imports files directly.
- Do NOT add scenario files to `tests/` — they live in `qa_scenarios/` only.
