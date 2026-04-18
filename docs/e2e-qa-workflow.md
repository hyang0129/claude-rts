# E2E QA Workflow

Automated end-to-end QA for new features using a team of agents. This workflow catches integration bugs that unit tests miss — frontend/backend wiring, DOM interactions, state across page reloads, and real external dependencies (Docker, filesystem, network).

See [feature-testing-guide.md](feature-testing-guide.md) for the lower layers (unit tests, puppeting API, log inspection) that this workflow builds on top of.

---

## When to use

- New user-facing feature with frontend + backend components
- Feature adds new card types, widgets, or UI flows
- Feature modifies existing E2E-covered behavior (regression risk)
- Complex API + UI integration that unit tests cannot fully exercise

**Skip when:** the change is backend-only with no UI, or is purely config/docs.

---

## Agent team

Five agents run sequentially. Agents 4 and 5 loop until tests pass or the max iteration limit is hit.

```
Agent 1 (Analyst)
  │
  ▼
Agent 2 (Designer)
  │
  ▼
Agent 3 (Implementer)
  │
  ▼
Agent 4 (Runner) ◄──┐
  │                  │
  ▼                  │
Agent 5 (Fixer) ─────┘
  │                 (max 3 rounds)
  ▼
Agent 4 (Runner) ← final pass
  │
  ▼
Report
```

All agents run on Opus. Each agent is a separate subagent with a fresh context window — no accumulated context drift.

---

## Agent 1 — Analyst

**Role:** Identify what needs to be tested and evaluated.

**Input:** Issue number, PR diff, branch name.

**Process:**
1. Read the issue, PR description, and refined spec (if one exists in `.claude-work/`)
2. Read the changed files to understand what was built
3. Identify user-facing behaviors that need verification
4. Identify external dependencies (Docker, filesystem, network, config persistence)
5. Classify each dependency as **real** or **mock** (see [Real vs. mock](#real-vs-mock) below)

**Output:** `.claude-work/E2E_<FEATURE>_ANALYSIS.md`

```markdown
# E2E Analysis: <feature>

## Behaviors to verify
- <behavior 1>
- <behavior 2>

## External dependencies
| Dependency | Real or mock | Justification |
|---|---|---|
| Docker containers | Real | Feature starts/stops containers — must test real Docker responses |
| Config file persistence | Real | Feature reads/writes config — must survive reload |
| CDN (xterm.js) | Mock | Out of scope, not testing terminal rendering |

## Risk areas
- <integration point that is most likely to break>
```

---

## Agent 2 — Designer

**Role:** Design E2E test scenarios based on the analysis.

**Input:** The analysis from Agent 1, plus access to existing E2E tests for pattern reference.

**Process:**
1. Read existing E2E tests (`tests/e2e/`) to understand patterns, fixtures, and infrastructure
2. Read new unit tests to understand what is already covered — do not duplicate
3. Design scenarios that exercise **frontend-backend integration**
4. One scenario per distinct user flow, not per feature
5. Prioritize: critical > high > medium > low

**Output:** `.claude-work/E2E_<FEATURE>_PLAN.md`

```markdown
# E2E Test Plan: <feature>

## Test Infrastructure Notes
<fixtures, test containers, setup/teardown needed>

## Scenarios

### S1: <name>
- **Setup**: <preconditions — containers to spin up, config to seed>
- **Steps**: <numbered user actions>
- **Expected**: <observable outcomes to assert>
- **Priority**: critical / high / medium / low
- **Real deps**: <which external systems are exercised for real>
```

---

## Agent 3 — Implementer

**Role:** Write the E2E test code and any supporting infrastructure.

**Input:** The test plan from Agent 2.

**Process:**
1. Read existing E2E test files to match patterns exactly (fixtures, conftest, assertions)
2. Implement all critical and high priority scenarios; include medium/low if straightforward
3. Set up real dependencies (spin up test containers, seed config files)
4. Add teardown logic to clean up real resources after tests
5. Run linter/formatter — do NOT run the tests yet

**Output:**
- Test file(s) in `tests/e2e/`
- Any test infrastructure (fixtures, helper functions, container setup scripts)
- Dev preset if needed (under `claude_rts/dev_presets/`)

**Constraints:**
- Match existing test patterns exactly — do not invent new conventions
- Real dependencies must have deterministic setup and teardown
- Any test-only server endpoints must be gated behind test mode

---

## Agent 4 — Runner

**Role:** Execute E2E tests, diagnose failures, produce a bug report.

**Input:** Path to E2E test file(s), round number (R1, R2, R3).

**Process:**
1. Kill any running server instances (follow the server rule in CLAUDE.md)
2. Run unit tests first to verify baseline is clean
3. Run E2E tests
4. For each failure:
   - Read the error message and traceback
   - Read the relevant source code to understand the root cause
   - Classify as **app-bug** (source code wrong) or **test-bug** (test code wrong)
5. On the final round, also run existing E2E smoke tests as a regression check

**Output:** `.claude-work/E2E_BUG_REPORT_R<N>.md`

```markdown
# E2E Bug Report — Round <N>

## Test Results Summary
- Passed: N
- Failed: N
- Errors: N

## Bugs Found

### BUG-R<N>-1: <title>
- **Test**: <test class/method>
- **Type**: app-bug | test-bug
- **Error**: <brief error message>
- **Root cause**: <analysis after reading source>
- **Fix**: <what needs to change and where>
- **Severity**: critical | high | medium
```

**Key principles:**
- Don't just report errors — **diagnose root causes** by reading source code
- A single root cause can cascade across many tests (especially with module-scoped fixtures) — identify the root, don't list each test separately
- Distinguish app bugs (ship-blocking) from test bugs (test code needs fixing)

---

## Agent 5 — Fixer

**Role:** Apply fixes from the bug report.

**Input:** Bug report from Agent 4.

**Process:**
1. Read the bug report
2. Read the relevant source files before making changes
3. Fix all bugs (both app-bugs and test-bugs)
4. Run linter/formatter on modified files
5. Run unit tests to verify nothing broke
6. Do NOT run E2E tests — hand back to Agent 4

**Constraints:**
- Fix only what the bug report identifies — no unrelated refactoring
- For app-bugs: minimal fix. If non-trivial, document the approach
- For test-bugs: fix the test to correctly exercise the behavior, don't weaken assertions

---

## Loop protocol

```
Round 1: Agent 4 (Runner) → Agent 5 (Fixer)
Round 2: Agent 4 (Runner) → Agent 5 (Fixer)
Round 3: Agent 4 (Runner) — final, report only, no more fixes
```

**Rules:**
- **Max 3 rounds.** If tests still fail after round 3, report outstanding issues and move on
- After each Fixer pass, the next Runner starts with a fresh context
- If a round finds 0 bugs, skip remaining rounds and report success
- The final Runner round must also run existing E2E/smoke tests as a regression check

---

## Real vs. mock

**Default to real.** If the feature interacts with an external system and that interaction is what we're testing, use the real thing.

### Use real when

- The feature starts/stops/queries Docker containers — spin up a test container
- The feature reads/writes config files — use real filesystem with temp dirs
- The feature makes HTTP calls to its own server — use the real running server
- The feature exercises a subprocess (`docker ps`, `docker start`) — run the real command
- The external system has multiple outcomes that matter (success, failure, timeout, partial response)

**Example:** VM Manager calls `docker start <name>`. The test should start a real stopped container and verify it transitions to running. It should also test starting a non-existent container and verify the error is surfaced to the user.

### Use mock when

- The dependency is out of scope for this feature (e.g. CDN assets, third-party APIs)
- The real system is unavailable in CI and the interaction is not what we're testing
- The dependency is already thoroughly covered by unit tests and adding real infra would be high cost for low signal

**Example:** If testing a widget's drag/resize behavior, mock the data endpoint — the test is about the UI interaction, not the data.

### Test container pattern

For features that need Docker containers:

```python
@pytest.fixture(scope="module")
def test_container():
    """Spin up a lightweight container for E2E tests."""
    name = "e2e-test-" + uuid.uuid4().hex[:8]
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "alpine:latest", "sleep", "3600"],
        check=True, capture_output=True,
    )
    yield name
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
    )
```

Always clean up test containers in fixture teardown. Use `alpine:latest` or similar minimal images to keep spin-up fast.

---

## Final report

After the last Runner round, the orchestrator presents:

```markdown
## E2E QA Report: <feature>

### Results by round
| Round | Passed | Failed | App bugs | Test bugs |
|-------|--------|--------|----------|-----------|
| R1    | N      | N      | N        | N         |
| R2    | N      | N      | N        | N         |
| R3    | N      | N      | N        | N         |

### App bugs found and fixed
- <title> — <one-line fix description>

### Test bugs found and fixed
- <title> — <one-line fix description>

### Outstanding issues (not fixed after 3 rounds)
- <title> — <severity, root cause, suggested fix>

### Dependency coverage
| Dependency | Real or mock | Notes |
|---|---|---|
| Docker containers | Real | Test container spun up/torn down per module |
| Config persistence | Real | Temp dir via tmp_path fixture |
| ... | ... | ... |

### Regression check
- Existing E2E smoke tests: N/N passed
- Unit tests: N/N passed

### Verdict: PASS | FAIL | PARTIAL (N outstanding issues)
```

---

## Artifacts

All intermediate artifacts are written to `.claude-work/` (git-excluded):

| File | Agent | Contents |
|------|-------|----------|
| `E2E_<FEATURE>_ANALYSIS.md` | Analyst | Behaviors, dependencies, risk areas |
| `E2E_<FEATURE>_PLAN.md` | Designer | Test scenarios with setup/steps/expected |
| `E2E_BUG_REPORT_R<N>.md` | Runner | Per-round bug reports with root cause analysis |
| `E2E_FINAL_REPORT.md` | Runner (final) | Consolidated results and verdict |
