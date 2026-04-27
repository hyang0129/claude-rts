# QA Scenarios — Runnable Discharge Path for QA Debt

This document describes the scenario format, naming convention, authoring contract,
and verdict semantics for `python -m claude_rts qa next`.

See issue #277 for the design rationale.

---

## Overview

`/e2e-qa` records residual human-QA items as markdown checkboxes on a debt issue
(e.g. #221).  The *discharge path* converts those items into runnable scenarios:
a coding agent authors a Playwright Python file, and the human runs `qa next` to
be driven to the judgment gate.

```
qa_scenarios/<id>.py   — Playwright scenario file (authored by coding agent)
                │
                ▼
python -m claude_rts qa next
                │
                ├─ launches app (--dev-config <preset>)
                ├─ drives Playwright to gate state
                ├─ prints HumanGate question
                ├─ reads y / n / s keystroke
                ├─ appends verdict to ~/.supreme-claudemander/qa-verdicts.jsonl
                └─ posts GitHub comment on debt_issue (y/n only)
```

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
| `scenario_id` | `str` | Yes | Unique identifier used as the JSONL key and in GitHub comments |
| `debt_issue` | `int` | Yes | GitHub issue number where the verdict comment will be posted |
| `preset` | `str` | No | Dev-config preset name (default: `"default"`) |

### `run_setup(page) -> HumanGate`

- Called with a Playwright `Page` already navigated to the running app.
- Must use **real app pathways only** — no `/api/test/...` endpoints.
- Must return a `HumanGate(scenario_id, question, expected)`.
- The browser window stays open until the human answers at the gate.

---

## Naming convention

Files are sorted **alphabetically** to determine run order.  Use the prefix
`<type>-<NNN>-<slug>.py` to encode priority:

| Prefix | Meaning |
|---|---|
| `syn-NNN-` | Synthetic scenario (not tied to a specific PR) |
| `pr<N>-sN-` | Scenario for PR #N, item N in that PR's checklist |

Examples:
- `syn-001-container-terminal.py` — first synthetic scenario
- `pr220-s1-container-label.py` — PR #220, item 1

---

## Verdict semantics

| Key | Stored in JSONL | GitHub comment posted |
|---|---|---|
| `y` | Yes (`verdict: "yes"`) | Yes — "PASS ✓" |
| `n` | Yes (`verdict: "no"`) | Yes — "FAIL ✗" |
| `s` | Yes (`verdict: "skip"`) | **No** |

A scenario is **discharged** when it has a JSONL record with `verdict in {"yes", "no"}`.
Skipped scenarios re-appear on every subsequent `qa next` invocation until they
receive `y` or `n`.

### Verdicts file schema

`~/.supreme-claudemander/qa-verdicts.jsonl` — one JSON object per line:

```json
{
  "scenario_id": "syn-001-container-terminal",
  "commit_sha": "abc1234",
  "verdict": "yes",
  "timestamp": "2026-04-27T16:00:00+00:00",
  "question": "Does the new terminal card appear on the canvas?"
}
```

---

## Running scenarios

```bash
# Run the next unverified scenario (headed browser — human can see the app)
python -m claude_rts qa next

# Run headless (wiring test only — human cannot see the UI)
HEADED=0 python -m claude_rts qa next
```

Prerequisites:
```bash
pip install -e ".[e2e]"
python -m playwright install chromium
```

---

## Authoring a new scenario

1. Identify a debt item from the linked `qa-debt` issue.
2. Create `qa_scenarios/<prefix>-<NNN>-<slug>.py` with a `Scenario` class.
3. Implement `run_setup(page)` using real app pathways (no test endpoints).
4. Smoke-check: run `HEADED=0 python -m claude_rts qa next` to verify Playwright
   reaches the gate state without error (keystroke prompt appears).
5. Commit the scenario file.

The coding agent handles steps 1–4; the human only needs to judge at the gate.

---

## What NOT to do

- Do NOT call `/api/test/...` endpoints in `run_setup()`.
- Do NOT add `auto_start: true` to a preset config just for a scenario — use
  the existing `human-qa` or `default` preset unless a new preset is strictly required.
- Do NOT write `qa_scenarios/__init__.py` — the runner imports files directly.
- Do NOT add scenario files to `tests/` — they live in `qa_scenarios/` only.
