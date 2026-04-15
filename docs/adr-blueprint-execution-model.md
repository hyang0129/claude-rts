# ADR: Blueprint System — Card and Canvas Level Initialization

## Status: ACCEPTED

## Context

supreme-claudemander is an RTS-style terminal canvas where devcontainer shells appear as draggable, resizable cards on a 4K canvas. The system uses a Python/aiohttp backend (`server.py`), a single-file HTML frontend (`static/index.html`), WebSocket-based real-time PTY streaming, and an EventBus for cross-card pub/sub. Cards are Python objects managed server-side by `CardRegistry`. The frontend is a rendering layer that reacts to server-pushed events over `/ws/control`.

Today, setting up a multi-card workspace is entirely manual. A user who wants three Claude Code terminals across two containers must: start each container, wait for it to come online, open terminals one at a time, and type the Claude startup commands into each. VM Manager actions partially automate per-container button clicks, but there is no way to orchestrate a full multi-card, multi-container workspace setup in a single operation.

The blueprint system solves this by providing a declarative, replayable artifact that can bring a canvas from empty to a fully populated workspace through a single trigger.

### What the current system can do

- **Start/stop containers** via `POST /api/vms/{name}/start|stop`
- **Discover containers** via `GET /api/vms/discover`
- **Spawn terminal cards** via `POST /api/claude/terminal/create` with `cmd`, `hub`, `container`, layout params, and `${priority_credential}` interpolation
- **Spawn Canvas Claude cards** via `POST /api/canvas-claude/create` with `profile`, `container`, layout params
- **Read priority profile** via `GET /api/profiles/priority`
- **EventBus** emits `card:registered` / `card:unregistered` events and fans out to `/ws/control` WebSocket clients
- **CARD_TYPE_REGISTRY** on the frontend provides `spawn()`, `deserialize()`, and `_mount()` for typed card instantiation
- **ServiceCard** base class supports cards that perform a job and emit results via EventBus
- **MCP server** exposes `open_terminal`, `vm_start_container`, `vm_discover_containers`, etc. as JSON-RPC tools for Claude Code agents running inside the canvas

### What the current system cannot do

- Sequence multiple canvas operations as a single atomic workflow
- Inject resolved variables (e.g., a discovered credential) into downstream steps
- Provide a pre-execution preview of what a workflow will do
- Produce a structured execution trace of what a workflow did

---

## Decisions

### Decision 1: Within-Card vs. Between-Cards Split

**Decision**: A blueprint has two wholly separate execution components that never overlap in responsibility.

**Between-cards** (canvas-level orchestration): A typed, declarative step list with a closed capability set. Steps produce named output variables that feed into subsequent steps. Executed server-side by the BlueprintCard.

**Within-card** (card-level initialization): A one-liner shell command embedded in the step action, injected into the terminal's PTY at spawn time. Inspired by SLURM job scripts — keep the submission line small, delegate complexity to externally-maintained scripts fetched at runtime. The BlueprintCard has no visibility into what runs after injection.

For simple cases the one-liner is the whole init:
```json
{ "action": "open_terminal", "container": "$container_name",
  "cmd": "cd /workspaces/hub3 && exec env CLAUDE_CONFIG_DIR=/profiles/$credential claude" }
```

For complex cases the one-liner fetches and runs an externally maintained script:
```json
{ "action": "open_terminal", "container": "$container_name",
  "cmd": "git clone git@github.com:me/dotfiles /tmp/df && bash /tmp/df/init-hub.sh" }
```

Within-card one-liner init is only valid for `open_terminal`. `open_claude_terminal` relies solely on `CLAUDE_CONFIG_DIR` and startup flags already built into `CanvasClaudeCard` — no script injection into the live Claude TUI. A custom subclass with TUI monitoring is a future concern, out of scope for v1.

**Note**: `CLAUDE_CONFIG_DIR` is the correct env var for Claude Code profile selection. No `--profile` CLI flag exists.

---

### Decision 2: BlueprintCard as a First-Class Server-Side Card

**Decision**: A blueprint, when spawned, becomes a **BlueprintCard** — a server-side Python card that extends `BaseCard` (or `ServiceCard`), registered in `CardRegistry`, and visible on the canvas. It is not a frontend module or a separate engine process.

The BlueprintCard:
- Appears on the canvas with its own draggable/resizable UI showing live execution progress
- Reads its declared parameters at spawn time (push-model context injection)
- Executes the between-cards step list server-side as async Python logic
- Subscribes to EventBus to receive signals from transient service cards it spawns
- Emits `blueprint:*` events that the frontend receives via `/ws/control` and renders in real time
- Remains on canvas after completion as a record of the run (or closes itself — TBD per step 8)

**Rationale**: Cards are the fundamental unit of the system. The blueprint orchestrates cards — it is itself a card. This unifies the model: no separate engine tier, no frontend orchestration logic. All stateful resources (PTYs, tmux sessions, CardRegistry) live on the server; the BlueprintCard is a peer to those resources, not a caller from outside.

**Future chaining**: A BlueprintCard can subscribe to another BlueprintCard's `blueprint:completed` event. The path to chaining is a natural extension of this model — no architectural rework needed.

---

### Decision 3: Transient Service Cards as Coordination Primitive

**Decision**: Steps that require waiting for an external condition (e.g., container readiness) are delegated to **transient service cards** — short-lived server-side cards that have one job, emit a result on the EventBus, and close themselves.

The `ContainerStarterCard` is the primary example:
1. BlueprintCard spawns a `ContainerStarterCard` for the target container
2. BlueprintCard subscribes to `container:ready:{name}` / `container:failed:{name}` on EventBus before spawning
3. `ContainerStarterCard` starts the container then probes readiness via `docker exec <name> true` (retried with backoff until success or timeout)
4. `ContainerStarterCard` emits `container:ready:{name}` or `container:failed:{name}` with result payload
5. `ContainerStarterCard` closes itself (unregisters from CardRegistry)
6. BlueprintCard receives the event and proceeds to the next step (or halts on failure)

This pattern:
- Solves `wait_container_ready` without polling logic in the BlueprintCard itself
- Makes each waiting concern independently testable
- Provides a visible card on canvas during the wait (user sees "starting hub3...")
- Is extensible: any new waiting concern gets a new transient service card type

**Readiness probe**: `docker exec <name> true` retried with exponential backoff. `state == "online"` from `GET /api/vms/discover` is not sufficient — it reflects Docker daemon state, not exec-readiness. The probe verifies exec-readiness directly.

---

### Decision 4: Step List Format, Variable Binding, and Step Completion

**Decision**: Between-cards steps are a JSON array of objects. Each step has an `action` string (from the closed capability set), action-specific parameters, and an optional `out` key naming the variable that receives the step's result. Subsequent steps reference prior outputs via `$variable_name` syntax (interpolated within string values).

**Step completion** is defined per action type:

| Action | Completes when | Output |
|---|---|---|
| `get_priority_profile` | API returns value | profile name string |
| `discover_containers` | API returns list | container list |
| `start_container` | `ContainerStarterCard` emits `container:ready` | container name |
| `open_terminal` | Server API returns `session_id` | session_id + card descriptor |
| `open_claude_terminal` | Server API returns `session_id` | session_id + card descriptor |
| `open_widget` | Frontend renders card (via `blueprint:open_widget` → `/ws/control` → frontend ack) | card id |

Within-card one-liners are fire-and-forget from the BlueprintCard's perspective — the step completes on API success, not when the one-liner finishes executing inside the PTY. If step N+1 genuinely depends on step N's one-liner completing something, that dependency is expressed within the one-liners themselves (e.g., a sentinel file, a port check).

Example step list:
```json
[
  { "action": "get_priority_profile", "out": "credential" },
  { "action": "start_container", "container": "hub3", "out": "container_name" },
  { "action": "open_claude_terminal", "container": "$container_name",
    "inject": { "credential": "$credential", "hub": "hub3" } }
]
```

---

### Decision 5: Sequential Orchestration and Failure Handling

**Decision**: The step list executes strictly sequentially — each step completes (or times out) before the next begins. No parallel execution within a single BlueprintCard run.

On failure: halt immediately at the failed step. Mark subsequent steps as not-run. **Leave already-spawned cards open — no rollback.** The BlueprintCard's UI shows exactly which step failed and why.

Future parallelism is handled by a **spawner card** type — a card that fans out a blueprint across N targets simultaneously. The BlueprintCard stays simple; parallelism is a card type responsibility.

**Per-step timeouts**: Implemented via `asyncio.wait_for`. Each action type has a default timeout; the step can override it with a `"timeout"` field.

---

### Decision 6: Dynamic Spawn Plan

**Decision**: The final canvas state after a BlueprintCard run cannot be statically determined — a step may spawn a transient service card or a future spawner card that produces a variable number of downstream cards.

The pre-spawn preview shows the **resolved step list** (what the blueprint will attempt), not a final card count. The **BlueprintCard's execution trace** is the authoritative record of what actually happened.

---

### Decision 7: Parameter Provenance Model

**Decision**: Push-model injection. At spawn time, the canvas assembles a typed context object and injects it into the BlueprintCard. Every parameter must be declared with explicit provenance:

- **User-supplied**: provided at blueprint invocation time (e.g., branch name, target path)
- **Canvas-context-injected**: resolved from canvas state at spawn time (e.g., `priority_credential`, discovered container names)
- **Static default**: baked into the blueprint definition

Undeclared use of canvas state is a pre-spawn validation error. The pre-spawn preview shows resolved values ("will use credential: alice"), not just parameter names.

---

### Decision 8: Execution Trace as BlueprintCard UI

**Decision**: The BlueprintCard's rendered body on the canvas **is** the execution trace — a live, step-by-step view updated in real time as steps complete. No separate trace panel is needed; the card IS the trace.

The BlueprintCard emits `blueprint:step_started`, `blueprint:step_completed`, `blueprint:step_failed` events on EventBus. These fan out via `/ws/control` to the frontend, which updates the BlueprintCard's rendered body. Each step shows: action name, resolved inputs, result or error, timing.

Server-side trace state lives on the BlueprintCard instance. Persistence (to disk) is deferred — the trace is available for the lifetime of the server process.

---

### Decision 9: Iteration in v1, Chaining Not

**Decision**: A blueprint can be parameterized with a list and expand across it (e.g., open a Claude terminal in each of N containers). Blueprint-to-blueprint invocation is explicitly out of scope for v1, but the BlueprintCard model naturally enables it: a BlueprintCard can spawn another BlueprintCard and subscribe to its `blueprint:completed` event.

---

## Open Questions

### 1. ~~Blueprint and `.sh` storage~~ — RESOLVED
See Decision 1 (one-liner inline in step action, blueprints in `~/.supreme-claudemander/blueprints/{name}.json`).

### 2. ~~`wait_container_ready` readiness signal~~ — RESOLVED
See Decision 3 (`ContainerStarterCard` with `docker exec <name> true` probe).

### 3. ~~`open_claude_terminal` vs. `open_terminal` — `.sh` injection~~ — RESOLVED
See Decision 1 (within-card one-liner only for `open_terminal`; `open_claude_terminal` uses `CLAUDE_CONFIG_DIR` only).

### 4. ~~Blueprint engine location~~ — RESOLVED
See Decision 2 (BlueprintCard is a server-side card; frontend is rendering layer only).

### 5. ~~`$variable` validation scope~~ — RESOLVED

See Decision 10. Substring interpolation allowed in string values; `$$` escapes a literal `$`; numeric fields disallow `$variable` references.

### 6. ~~For-each iteration format~~ — RESOLVED

See Decision 9. A dedicated `for_each` step type containing a sub-list of steps.

### 7. ~~Blueprint UI integration~~ — RESOLVED

See Decision 11. Canvas context menu with a "Blueprints" submenu (max 10 entries). Exact initialization path deferred; architecture takes priority over trigger surface.

### 8. ~~BlueprintCard lifetime~~ — RESOLVED

See Decision 12. BlueprintCard closes itself on completion. Execution log is persisted server-side. A Blueprint Manager widget (for browsing past runs) is a follow-up issue, out of scope for v1.

---

### Decision 10: `$variable` Interpolation Rules

**Decision**: `$variable` references are allowed anywhere inside a string value, including substrings (e.g., `"cmd": "cd /work/$branch && claude"`). This matches Bash, Docker Compose, and GitHub Actions conventions, so users already understand the mental model.

Rules:
- `$$` is the escape sequence for a literal `$` character.
- `$variable` in a numeric field (e.g., `"cols": "$width"`) is a pre-spawn validation error — no silent type coercion.
- Unresolvable references (variable not declared or not yet bound) are caught at pre-spawn validation, not at runtime.
- Variable names follow `[a-zA-Z_][a-zA-Z0-9_]*` — identical to Bash identifier syntax.

---

### Decision 11: Blueprint Trigger UI

**Decision**: Blueprints are triggered from the canvas **right-click context menu** under a "Blueprints" submenu. The submenu shows up to 10 blueprints by name; selecting one opens a parameter-fill dialog (for user-supplied params) before spawning.

The exact frontend initialization surface (dialog, sidebar form, etc.) is a UX detail — the architecture depends only on the submenu entry dispatching `POST /api/blueprints/spawn`. No new `CARD_TYPE_REGISTRY` entry is required for the trigger itself; the BlueprintCard that appears on canvas is registered as a card type in the usual way.

A dedicated Blueprint Manager widget (sidebar panel for browsing, editing, and importing blueprints) is a follow-up, out of scope for v1.

---

### Decision 12: BlueprintCard Lifetime and Logging

**Decision**: The BlueprintCard **closes itself** on completion (success or failure). It does not remain as a persistent canvas artifact.

Execution is logged server-side (append-only log file or structured store — format TBD) so runs can be reviewed after the card closes. The BlueprintCard's canvas body, while alive, shows **plain log output** (timestamped lines) rather than a rich step-by-step visualization. A dedicated execution trace / visualization UI is a follow-up issue, explicitly out of scope for v1.

This keeps v1 minimal: the card does its job, logs what happened, and gets out of the way.

---

## Consequences

### `server.py`
- New route: `POST /api/blueprints/spawn` — creates a BlueprintCard, begins execution
- New route: `GET/POST/PUT/DELETE /api/blueprints` — CRUD for blueprint definitions
- New route: `POST /api/blueprints/validate` — pre-spawn validation, returns resolved step list preview
- `open_widget` steps require a new frontend coordination mechanism: BlueprintCard emits `blueprint:open_widget` event, frontend calls `CARD_TYPE_REGISTRY.spawn('widget', ...)` and acks back

### Card hierarchy (`claude_rts/cards/`)
- **New**: `BlueprintCard(BaseCard)` — server-side orchestrator card, manages step list execution, subscribes/unsubscribes EventBus per step
- **New**: `ContainerStarterCard(ServiceCard)` — transient card, probes container readiness, emits `container:ready|failed`, self-closes
- `BaseCard`, `TerminalCard`, `CanvasClaudeCard`, `CardRegistry` unchanged

### Frontend (`static/index.html`)
- Canvas right-click context menu: "Blueprints" submenu (up to 10 entries), triggers `POST /api/blueprints/spawn`
- BlueprintCard rendering: receives `blueprint:log` events via `/ws/control`, appends timestamped log lines to card body (plain log, not step visualization — visualization is a follow-up)
- `open_widget` coordination: listens for `blueprint:open_widget` on `/ws/control`, calls `CARD_TYPE_REGISTRY.spawn('widget', ...)`, sends ack

### EventBus
- New namespaces:
  - `blueprint:log` — timestamped log line emitted by BlueprintCard during execution (frontend appends to card body)
  - `blueprint:completed`, `blueprint:failed` — terminal events; BlueprintCard self-closes after emitting
  - `container:ready:{name}`, `container:failed:{name}` (from ContainerStarterCard)
- `blueprint:step_started/completed/failed` events (fine-grained) deferred to follow-up visualization issue
- Frontend subscribes via existing `/ws/control` WebSocket

### Data model
- `~/.supreme-claudemander/blueprints/{name}.json`
- Blueprint schema: `{ name, description, parameters: [{name, provenance, type, default?}], steps: [{action, ...}] }`
- Execution trace (in-memory on BlueprintCard): `{ run_id, blueprint_name, started_at, steps: [{action, inputs, output, status, started_at, duration_ms}] }`

### Existing VM Manager actions
- Unchanged and independent. Blueprints are a higher-level orchestration that may call the same underlying APIs.

### Tests
- New `test_blueprint_card.py`: BlueprintCard step execution, variable binding, failure halting, pre-spawn validation, EventBus subscription lifecycle
- New `test_container_starter_card.py`: readiness probe, success/failure emission, self-close
- Integration tests: multi-step blueprint run with MockPty and mock Docker responses
- E2E: blueprint spawn from UI, live trace rendering in BlueprintCard body

---

## Updated Acceptance Criteria

- [ ] Blueprint JSON schema defined and documented (parameters + steps format)
- [ ] Blueprint definitions stored in `~/.supreme-claudemander/blueprints/{name}.json`
- [ ] Blueprint CRUD API: list, get, create, update, delete
- [ ] `POST /api/blueprints/validate` returns resolved step list preview with parameter values
- [ ] `BlueprintCard` registers in `CardRegistry` and appears on canvas when spawned
- [ ] BlueprintCard executes steps sequentially with `$variable` binding
- [ ] `get_priority_profile` step retrieves current priority profile
- [ ] `discover_containers` step returns container list
- [ ] `start_container` step spawns a `ContainerStarterCard` and waits for `container:ready` event
- [ ] `ContainerStarterCard` probes readiness via `docker exec <name> true`, emits result, self-closes
- [ ] `open_terminal` step spawns a TerminalCard, completes on API success
- [ ] `open_claude_terminal` step spawns a CanvasClaudeCard with `CLAUDE_CONFIG_DIR` injection
- [ ] Within-card one-liner is injected as the terminal's startup command
- [ ] Failure halts execution, leaves spawned cards open, emits `blueprint:failed` log line
- [ ] BlueprintCard body shows timestamped log output updated via `blueprint:log` events on `/ws/control`
- [ ] BlueprintCard closes itself after emitting `blueprint:completed` or `blueprint:failed`
- [ ] Execution is logged server-side (append-only) so runs are reviewable after card closes
- [ ] `open_widget` step coordinates with frontend via `blueprint:open_widget` event
- [ ] `for_each` step type expands a sub-list of steps across a list variable
- [ ] Canvas right-click context menu has a "Blueprints" submenu listing available blueprints (max 10)
- [ ] `$variable` interpolation works inside substrings; `$$` produces a literal `$`; numeric fields with `$variable` are rejected at validation
- [ ] Unit tests cover BlueprintCard step execution, variable binding, failure modes, EventBus lifecycle
- [ ] Integration test covers a multi-step blueprint run end-to-end
