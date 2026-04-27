# Making issue #221's human-only QA convenient

## The mistake we just made

The first attempt at #221 wrote Playwright/real-Docker tests for the four
"why human" items in the QA debt backlog. That defeats the purpose. Each item
was tagged human-only because it requires *judgment* the test harness can't
make:

| Item | What needs judgment |
|---|---|
| S1 — label says "Container Manager" | A human reading the rendered widget. (DOM string-match doesn't catch "the label is technically right but is rendered illegible / clipped / wrong colour".) |
| S2 — `container_create` via MCP in a Canvas Claude card | The full live MCP-over-tmux path through a real Claude session. Calling the REST endpoint directly skips the very integration the human is verifying. |
| S3 — Container Stats live refresh | A human watching numbers update at a believable cadence. Asserting "the JS interval property is 5000" doesn't observe whether the user *sees* refresh happen. |
| S4 — `/profiles` mount inside a created container | The full create-via-UI-or-MCP flow ending with a human seeing files. `docker exec ls /profiles` on a hand-rolled `docker run` doesn't exercise the create path. |

So the right question isn't "how do I automate these?" — it's **"how do I make
the cost of doing the human task fall below the cost of clicking 'accept' and
moving on?"**

## The skip equation

A lazy human will skip when:

```
cost(actually do task) > cost(click accept) + p(getting caught) × cost(getting caught)
```

There are three levers:

1. **Crush `cost(actually do task)`** — pre-stage everything, no setup, no
   context-switching, one click per scenario.
2. **Make skipping visible** — unanswered items must be loud. A silent
   checkbox in a markdown file in a separate repo is invisible; a banner in
   the dev preset that says "3 unanswered QA items from 5 days ago" creates
   social cost (even just internal cost) for skipping.
3. **Make `[No, it's broken]` as cheap as `[Yes]`** — if "fail" requires
   writing a bug report, every honest fail gets re-rationalised into a pass.

The current state of #221 (a markdown checklist in a backlog issue) loses on
all three. Setup cost: re-read PR #220, figure out the dev preset, spawn
widgets, exec into containers. Skip cost: zero. Bug-reporting cost: high.
Predicted human behaviour: skip → eventually click "looks fine" without
checking.

## Specific designs per item

### S1 — Container Manager label

- **Pre-stage**: a `qa-#221` dev preset boots with a Container Manager widget
  already on the canvas, top-left, already rendered.
- **Question card**: a small overlay card next to the widget, with text:
  > "Does the widget on the left say **Container Manager** in its title?
  > [Yes ✓]  [No ✗]"
- **Auto-corroborate**: the system *also* reads the DOM. If the DOM says
  "Container Manager" the [Yes] button is the lit option, but the human still
  has to click. Mismatch between human-click and DOM-corroboration → flag it
  loudly (the human caught a rendering bug the test missed).
- **Time-to-do**: ~5 seconds.

### S2 — `container_create` via MCP in a Canvas Claude card

This is the painful one because it requires a live Claude session.

- **Pre-stage**: preset boots with a Canvas Claude card already attached to a
  tmux session, with the prompt
  > "Use the `container_create` MCP tool to create a container named
  > `qa-s2-test` from `ubuntu:24.04`, then list favorites and report whether
  > the new container is there."

  pre-typed into the input but **not submitted**.
- **One-click run**: the question card has a "Run S2" button that submits the
  prompt and starts a 60-second observation timer.
- **Outcome question**: after the timer (or when Claude finishes), a card
  asks:
  > "Did `qa-s2-test` appear in the Container Manager widget? [Yes] [No]"
- **Cleanup is automatic**: the preset reset on next boot wipes the test
  container.
- **Time-to-do**: ~30 seconds, almost all of it watching.

### S3 — Container Stats live refresh

- **Pre-stage**: preset includes a *real* canvas-claude container running a
  CPU-fluctuating workload (e.g. `while true; do yes > /dev/null & sleep 2;
  kill %1; sleep 2; done`), plus the Container Stats widget already spawned
  next to it.
- **Visible refresh corroboration**: in dev mode, the widget body shows a
  small "last refresh: HH:MM:SS · count: N" footer. Refresh is unmistakable
  even to a glance.
- **Question card**:
  > "Watch the widget for 15 seconds. Does the count increment? Do the CPU
  > numbers move? [Yes] [No]"
- **Time-to-do**: ~20 seconds.

### S4 — `/profiles` mount inside a created container

- **Pre-stage**: preset has a terminal card already exec'd into a fresh
  canvas-claude container (created via the production `POST
  /api/containers/create` path on preset boot, not hand-rolled `docker run`).
  The terminal's `recovery_script` auto-runs `ls -la /profiles && cat
  /profiles/main/.credentials.json | head -3` so the human sees the output
  immediately.
- **Question card**:
  > "Did the terminal on the right list files (and not 'No such file')?
  > [Yes] [No]"
- **Time-to-do**: ~5 seconds.

## Cross-cutting ideas

### One preset, all four scenarios visible at once

Boot once → see four question cards in a row. Total human-time: <60 seconds
across all four. That's the only way it competes with click-accept.

### Yes/No buttons, never free-form text

Lazy humans don't type. They click. Every QA prompt should be a binary or
trinary choice (Yes / No / Skipped-for-now), never "describe what you saw".

### A persistent "QA debt" footer in dev mode

Show unanswered items at the bottom of the screen on every dev-mode boot:
> "QA debt: 3 unanswered items from #221 (oldest: 5 days ago)"

Loud, persistent, but dismissable per-session. Creates the visibility that
the markdown checklist doesn't have.

### Negative answers must be one-click too

`[No ✗]` can't open a "now write a bug report" form, or every honest [No] gets
rationalised into [Yes]. A click on [No] should: log the result, post a
templated comment on issue #221 with the scenario id and any auto-corroborated
data, and prompt the human only with a *one-line* "what did you see?" field
that's optional.

### Auto-corroboration where possible

Even on human-only items, the system can usually verify *something* (DOM
contents, API state, container existence). Surface that corroboration next to
the human's click — not as a substitute, but as a cross-check. If human
clicks [Yes] and corroboration says "actually, the DOM doesn't contain
'Container Manager'", flag the discrepancy. This catches both
dishonest-yes-clicks and rendering bugs the auto-check misses.

### Persist the verdicts

Store every QA verdict in `~/.supreme-claudemander/qa-debt-log.json` keyed by
scenario id + commit SHA. On future boots, if the same scenario passes
without any code change to the relevant area, you can decay the question
("you confirmed this 3 days ago, recheck or skip?"). If the relevant area
changed, re-prompt.

### Don't auto-close the issue

A passing run should comment on issue #221, not auto-close it. The human
closing the issue is itself a checkpoint — if the QA log says "all four
pass" but the human pauses before closing, that pause is information.

## Anti-patterns to avoid

- **Long markdown checklists.** Skip rate ~100%.
- **Multi-step instructions** ("first run X, then run Y, then check Z"). Each
  step is a context-switch and an opportunity to bail.
- **Pass/fail purely on the human's word.** Always corroborate something.
- **Requiring clean state.** "First wipe your config" → skip. The preset
  must own its own clean state via reset-on-boot.
- **Bundling QA into a 30-min "QA session" event.** That's how items end up
  6 weeks stale. Per-scenario, per-PR, in-line.
- **A separate QA tool / page / repo.** It must be in the dev preset the
  human is already using.

## What this would actually look like as a PR

(Not implementing now — just shape.)

1. **New dev preset `qa-debt`** with all four scenarios pre-staged on canvas
   boot.
2. **New "QA Card" type** — a small card with a question, Yes/No/Skip
   buttons, and an auto-corroboration footer.
3. **New endpoints** `POST /api/qa/answer/{scenario_id}` and
   `GET /api/qa/debt` for verdict storage and the persistent footer.
4. **Hook the preset's boot path** to spawn one QA Card per unresolved
   scenario, alongside the cards needed to evaluate it.
5. **Issue-#221 integration** — a "comment on issue" button on each QA Card
   posts a templated update via `gh api`.

The PR's value is that it makes the *next* QA-debt item (whatever PR #277
ships) cheap to verify, not just the four from #220. The four from #220 are
the test bed.

## TL;DR

The fix for "lazy human skips QA" isn't more automation — it's making the
human do less work *per scenario* than clicking "accept all", with enough
auto-corroboration that dishonest yes-clicks get caught. Pre-stage the
environment, single-click verdicts, persistent visibility of unanswered
debt, and equal-cost yes-vs-no answers.
