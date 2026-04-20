# supreme-claudemander

RTS-style terminal canvas — devcontainer shells as draggable, resizable cards on a pannable/zoomable 4K canvas.

## Server Rule

**Never run more than one server instance at a time.** Before starting a server, kill any existing one. The devcontainer has no `pkill`/`fuser`/`killall`, so scan `/proc` directly:

```bash
for pid in $(ls /proc/ | grep -E '^[0-9]+$'); do
  cmd=$(cat /proc/$pid/cmdline 2>/dev/null | tr '\0' ' ')
  echo "$cmd" | grep -q "python -m claude_rts" && kill "$pid"
done
sleep 1
# verify port 3000 is clear (0BB8 = 3000, 0A = LISTEN)
cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk '$2 ~ /:0BB8$/ && $4 == "0A"'
```

Verify the port is free before starting. Running multiple instances causes port conflicts, confusing log output, and stale probe sessions.

**Use `--electron` for manual/QA testing.** Launch via `python -m claude_rts --electron` to run in the Electron shell instead of a browser tab.

## Key Design Decisions

- **ptyprocess for POSIX PTY**: `ptyprocess` is the PTY backend on Linux/macOS. The Windows ConPTY branch (`pywinpty`) was removed — Windows is community-supported best-effort only.
- **Session persistence**: SessionManager decouples PTY lifetime from WebSocket. PTYs run in server memory with a 64KB scrollback ring buffer. Orphan reaper cleans up after 5 min.
- **Single HTML file**: All JS/CSS is inline in `index.html`. External libs (xterm.js) load from CDN. No npm, no bundler.
- **Card class hierarchy**: `Card` base → `TerminalCard`, `WidgetCard`, `LoaderCard`. Enables mixed dashboards.
- **Limited container lifecycle (start/stop only)**: supreme-claudemander can start and stop Docker containers via the Container Manager card. Creating, removing, and image management remain out of scope.
- **Plain `docker` binary**: Always use `docker` (no `.exe`). The runtime is Linux/macOS-native. Windows is community-supported best-effort.

## Dev Config Presets

`--dev-config [PRESET]` starts the server with an isolated config directory (`~/.supreme-claudemander-dev/`) that is wiped and rebuilt on every startup. Useful for testing specific features without touching the real user config.

```bash
python -m claude_rts --dev-config              # uses 'default' preset
python -m claude_rts --dev-config profiles     # uses 'profiles' preset
```

Presets are stored as JSON fixture files in `claude_rts/dev_presets/<name>/`:

```
claude_rts/dev_presets/
├── default/
│   ├── config.json
│   └── canvases/
│       └── probe-qa.json
└── profiles/
    ├── config.json
    └── canvases/
        └── profiles-dev.json
```

To add a new preset: create a directory under `dev_presets/` with a `config.json` and at least one canvas in `canvases/`. The preset is auto-discovered by name.

| Preset | Purpose |
|--------|---------|
| `default` | Bare util-terminal + empty probe-qa canvas |
| `profiles` | Profile Manager widget pre-placed on canvas |
| `start-claude` | Start Claude button QA — main profile slot (`main_profile_name: "main"`), Profile Manager widget on canvas. `/profiles/main/.credentials.json` is **not** pre-populated; promote `test-profile` via "Set as in-use" before clicking Start Claude, or the button surfaces the "no credentials yet" warning. The preset mounts `claude-profiles:/profiles` so credentials persist across preset restarts — delete the named volume (`docker volume rm claude-profiles`) to reset to a clean state. |
| `stress-test` | Layout QA — 6 cards at edge positions, varying sizes, overlapping z-order |
| `claude-api` | Claude terminal control API QA — empty canvas for programmatic terminal lifecycle |

## Testing

For E2E QA of new features, follow [`docs/e2e-qa-workflow.md`](docs/e2e-qa-workflow.md) — a 5-agent team workflow that designs, implements, and iterates on E2E tests using real dependencies (Docker containers, filesystem) by default.

```bash
python -m pytest tests/ -v
python -m ruff check . && python -m ruff format --check .   # lint + format check
CLAUDE_RTS_TEST_MODE=1 python -m claude_rts   # enables puppeting API at /api/test/...
```

**Always run `ruff format` before committing.** CI enforces formatting via `ruff format --check`.

**Wait for CI to pass before merging PRs.** All GitHub Actions checks (lint, format, tests) must be green.

### E2E Tests in the Devcontainer

The devcontainer is configured to run the full E2E suite including Docker-gated and browser tests:

- **Docker socket**: `postStartCommand` in `devcontainer.json` applies `chmod 666 /var/run/docker.sock` on every container start (not just creation). This is required because the socket is owned by `root:root GID=0` but the `docker` group inside the container has a different GID.
- **Chromium for Playwright**: Install once after the container is created:
  ```bash
  pip install -e ".[e2e]" && python -m playwright install chromium
  ```
- **Docker-gated helpers** (`_ensure_util_profile`, `_clear_main_slot`): These helpers use `--user root` when writing to `/profiles` inside the util container. `--user root` is used defensively because `/profiles` may be owned by `root:root` inside the named volume on first creation (Docker initialises named-volume directories from the image filesystem, which may set the directory to `root:root 755`); running as root ensures writes succeed regardless of volume state.
- **Stale util container state**: The util container persists `/profiles` between test runs. Tests that depend on the main slot being empty must call `_clear_main_slot()` at the start (see `test_no_main_credentials_shows_warning`).

| File | Tests | What it covers |
|------|-------|----------------|
| `test_discovery.py` | 6 | Docker hub discovery parsing |
| `test_server.py` | 7 | HTTP routes, widget endpoints, route registration |
| `test_main.py` | 4 | CLI argument handling |
| `test_config.py` | 23 | Config CRUD, canvas CRUD, API endpoints |
| `test_startup.py` | 7 | Startup scripts, API endpoint |
| `test_server_profiles.py` | 9 | Profile manager API endpoints |
| `test_dev_config.py` | 8 | Dev-config preset loading and setup |
| `test_sessions.py` | 30 | ScrollbackBuffer, SessionManager, test puppeting API |
| `test_terminal_card.py` | 18 | TerminalCard lifecycle, CardRegistry, server integration, display_name, recovery_script |
| `test_claude_api.py` | 38 | Claude terminal control API (CRUD, send/read, strip_ansi, /ws/control, full lifecycle, cmd pass-through, rename, recovery-script, card_updated broadcast) |
| `test_event_bus.py` | 14 | EventBus core (subscribe, emit, unsubscribe, wildcard, async, errors, clear) + integration (ServiceCard bus emit, CardRegistry events) |
| `test_container_manager.py` | 18 | Container Manager API (discover containers, favorites CRUD, start/stop container, per-container actions, route registration) |
| `test_mcp_server.py` | 64 | MCP server tool functions (terminal CRUD/rename/recovery + VM discover/favorites/actions/start/stop/append/get + blueprint list/get/save/delete/spawn) and JSON-RPC dispatch |
| `e2e/test_smoke.py` | 7 | Playwright Electron smoke tests — launch, spawn, drag, resize, widgets, pan/zoom, save/reload |

Tests use `MockPty` to avoid needing Docker. E2E tests require Playwright and Electron (`pip install -e ".[e2e]" && python -m playwright install chromium`).

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serves `static/index.html` |
| GET | `/api/hubs` | Discovered devcontainers |
| GET | `/api/startup` | Run startup script, return card descriptors |
| GET/PUT | `/api/config` | Read/write config |
| GET/PUT/DELETE | `/api/canvases/{name}` | Canvas layout CRUD |
| GET | `/api/sessions` | List active sessions |
| GET | `/api/widgets/system-info` | System info widget data |
| GET | `/api/profiles` | Probe profiles with usage data, sorted by burn rate |
| GET/PUT | `/api/profiles/main` | Read the main profile slot name / promote a tracked profile (credential copy) |
| GET | `/api/containers/discover` | Discover all Docker containers (running + stopped) with status |
| GET/PUT | `/api/containers/favorites` | Read/write Container Manager favorites list |
| POST | `/api/containers/{name}/start` | Start a stopped Docker container |
| POST | `/api/containers/{name}/stop` | Stop a running Docker container (optional `?timeout=N`) |
| PUT | `/api/containers/favorites/{name}/actions` | Update actions for a specific favorite container |
| POST | `/api/claude/terminal/create` | Create a TerminalCard + PTY session (params: cmd, hub, container, cols, rows, x, y, w, h). Optional: `ephemeral=true` (no card registered, auto-closes on PTY EOF or timeout), `spawner_id=<card_id>` (attributes the spawn to a CanvasClaudeCard for cap enforcement), `timeout=<seconds>` (ephemeral only; default 60, max 120; values outside `[1, 120]` return HTTP 400 `ephemeral_timeout_too_long`). When `spawner_id` is set and the spawner already has 10 live sessions, returns HTTP 429 `terminal_cap_reached` with `live_session_ids`. |
| POST | `/api/claude/terminal/{id}/send` | Write text to a terminal PTY |
| GET | `/api/claude/terminal/{id}/read` | Read scrollback (optional: strip_ansi, last_n) |
| GET | `/api/claude/terminal/{id}/status` | Session metadata (alive, age, idle, cmd) |
| PUT | `/api/claude/terminal/{id}/rename` | Set display name on a terminal card |
| GET/PUT | `/api/claude/terminal/{id}/recovery-script` | Read/set recovery script on a terminal card |
| DELETE | `/api/claude/terminal/{id}` | Stop card, clean up session and card registry |
| GET | `/api/claude/terminals` | List all terminal cards with metadata (includes display_name, recovery_script) |
| WS | `/ws/session/new?cmd=...` | Create persistent PTY session |
| WS | `/ws/session/{session_id}` | Reconnect to existing session |
| WS | `/ws/control` | Card lifecycle events (card_created, card_deleted) broadcast |

## WebSocket Protocol

- **Binary frames** (browser → server): terminal keystrokes (UTF-8)
- **Binary frames** (server → browser): terminal output or scrollback replay
- **Text frames** (server → browser): JSON control — `{"session_id": "..."}`, `{"error": "..."}`, `{"type": "session_attached"}`
- **Text frames** (browser → server): JSON control — `{"type": "resize", "cols": 120, "rows": 40}`

## Config

Stored in `~/.supreme-claudemander/config.json`:

```json
{
  "startup_script": "discover-devcontainers",
  "sessions": { "orphan_timeout": 300, "scrollback_size": 65536 },
  "util_container": { "name": "supreme-claudemander-util", "mounts": {} }
}
```

Canvas layouts stored in `~/.supreme-claudemander/canvases/{name}.json`.

## Container Manager Action Schema

Each favorite container has an `actions` array of blueprint-action objects. Actions spawn blueprints scoped to the container:

```json
{
  "label": "Dev Shell",           // Required: button label
  "blueprint": "dev-shell",       // Required: name of a saved blueprint
  "context": { "branch": "main" } // Optional: extra variables merged with auto-injected container name
}
```

When an action button is clicked, the frontend calls `POST /api/blueprints/spawn` with `{name: act.blueprint, context: {container: "<fav-name>", ...act.context}}`. The container name is always injected automatically. New favorites default to an empty actions array.

`POST /api/claude/terminal/create` passes its `cmd` query parameter through verbatim — no placeholder substitution is performed. To launch claude with the in-use credential, reference the main profile slot directly: `cmd=env CLAUDE_CONFIG_DIR=/profiles/<main_profile_name> claude` (resolve the slot name first via `GET /api/profiles/main`; defaults to `main`). The main slot is populated by clicking "Set as in-use" in the Profile Manager, which copies the selected profile's `.credentials.json` into `/profiles/<main_profile_name>/`.

## Canvas Claude MCP Tools

The Canvas Claude card (`claude_rts/mcp_server.py`) exposes a JSON-RPC stdio MCP server so Claude Code, running inside the card, can drive the canvas programmatically. Each tool is a thin wrapper around a REST endpoint.

### Canvas Claude 10-terminal cap

Each `CanvasClaudeCard` enforces a hard cap of 10 live terminals across both `open_terminal` and `run_task` spawns originating from that card. The 11th spawn returns HTTP 429 with `{"error": "terminal_cap_reached", "live_session_ids": [...]}`. The cap decrements on explicit `delete_terminal`, PTY EOF, and `run_task` timeout expiry. On card unregister, all sessions attributed to that spawner are cleaned up automatically. Terminals spawned directly by a user (no `spawner_id`) are not subject to this cap. **The cap applies to any spawn that carries `spawner_id`, regardless of `ephemeral` — a normal `open_terminal` from Canvas Claude also consumes a slot.**

| Tool | Wraps | Purpose |
|---|---|---|
| `open_terminal` | `POST /api/claude/terminal/create` | Spawn a new terminal card (supports `x, y, w, h, container, hub`). `cmd` is passed through verbatim; reference `/profiles/<main_profile_name>` directly to use the in-use credential (resolve the slot name via `GET /api/profiles/main`; defaults to `main`). |
| `run_task` | `POST /api/claude/terminal/create` (ephemeral=true) | Run a short-running command in an ephemeral PTY (no visible card, auto-closes on PTY EOF or timeout). Default timeout 60s; max 120s — longer operations must use `open_terminal`. Counts toward the 10-terminal cap. Rejects `timeout > 120` with structured error `ephemeral_timeout_too_long`. Use for: `ls`, `git pull`, probes, one-shot checks. |
| `read_terminal` | `GET /api/claude/terminal/{id}/read` | Read scrollback (default: strip ANSI) |
| `write_terminal` | `POST /api/claude/terminal/{id}/send` | Write keystrokes to a terminal |
| `list_terminals` | `GET /api/claude/terminals` | List active terminal cards |
| `delete_terminal` | `DELETE /api/claude/terminal/{id}` | Close a terminal card |
| `rename_terminal` | `PUT /api/claude/terminal/{id}/rename` | Set a display name on a terminal card |
| `set_recovery_script` | `PUT /api/claude/terminal/{id}/recovery-script` | Set recovery script on a terminal card |
| `get_recovery_script` | `GET /api/claude/terminal/{id}/recovery-script` | Get recovery script for a terminal card |
| `container_discover` | `GET /api/containers/discover` | List all Docker containers (running + stopped) |
| `container_get_favorites` | `GET /api/containers/favorites` | Read the Container Manager favorites list with blueprint-actions |
| `container_get_actions` | `GET /api/containers/favorites` (filtered) | Return one favorite's `actions` array; errors on unknown container |
| `container_set_actions` | `PUT /api/containers/favorites/{name}/actions` | Replace the full actions array for a favorite |
| `container_append_action` | `GET` + `PUT` (atomic) | Append one blueprint-action without dropping existing entries |
| `container_add_favorite` | `GET` + `PUT /api/containers/favorites` | Add a container to favorites (default: empty actions) |
| `container_start` | `POST /api/containers/{name}/start` | Start a stopped container |
| `container_stop` | `POST /api/containers/{name}/stop?via=canvas-claude` | Stop a running container (optional `timeout`). Guarded: server rejects with HTTP 403 `not_canvas_claude_owned` unless the container carries the Docker label `created_by=canvas-claude`. Human UI calls omit `via=canvas-claude` and are unguarded. |
| `blueprint_list` | `GET /api/blueprints` | List all saved blueprint names |
| `blueprint_get` | `GET /api/blueprints/{name}` | Get a single blueprint definition |
| `blueprint_save` | `PUT /api/blueprints/{name}` (upsert) | Save or update a blueprint definition |
| `blueprint_delete` | `DELETE /api/blueprints/{name}` | Delete a saved blueprint |
| `blueprint_spawn` | `POST /api/blueprints/spawn` | Spawn a BlueprintCard from a saved blueprint with context |

Removing favorites and creating/removing/pulling containers remain human-only operations (see "Limited container lifecycle" above).

## Adding a New Widget

Every widget requires exactly three coordinated changes:

1. **`server.py`** — add a handler `widget_{type}_handler()` and register it in `create_app()` under `/api/widgets/{type}`
2. **`index.html` → `WIDGET_REGISTRY`** — add a `'{type}': { label, defaultRefreshInterval, async render(body, card) {} }` entry; render fetches `/api/widgets/{type}` and writes `body.innerHTML`
3. **`index.html` → sidebar** — add a `<div class="widget-item" data-widget="{type}">` so the widget is spawnable

Do not create standalone pages, inline fetch calls outside `WIDGET_REGISTRY`, or ad-hoc refresh logic. The `WidgetCard` lifecycle (auto-refresh, state tracking, drag/resize, serialization) only works through the registry.

## EventBus

`EventBus` (`claude_rts/event_bus.py`) is the central pub/sub backbone for server-side cross-card communication. It lives at `app["event_bus"]` and is injected into cards via `BaseCard.bus`.

### Emitting events from a card

Any card subclass can emit:

```python
if self.bus is not None:
    await self.bus.emit("probe:claude-usage", result_dict)
```

ServiceCard does this automatically in `_notify_subscribers()` — after legacy callback delivery, it emits `probe:{card_type}`.

### Subscribing to events

From a handler, widget, or card:

```python
bus: EventBus = request.app["event_bus"]
bus.subscribe("probe:claude-usage", my_callback)
```

Callbacks receive `(event_type: str, payload: dict)`. They may be sync or async (async callbacks are fire-and-forget via `asyncio.create_task`). Exceptions are logged, never propagated.

Wildcard: `bus.subscribe("*", cb)` receives every event.

### Event naming conventions

Events follow `{namespace}:{action}` format:

| Event pattern | Emitter | Payload |
|---|---|---|
| `probe:{card_type}` | ServiceCard subclasses | Parsed probe result dict |
| `card:registered` | CardRegistry | `{card_id, card_type}` |
| `card:unregistered` | CardRegistry | `{card_id, card_type}` |
| `terminal:started` | TerminalCard (future) | `{session_id, cmd, hub, container}` |
| `terminal:stopped` | TerminalCard (future) | `{session_id}` |

New card types add their own namespaced events following this pattern.

### Shutdown

`event_bus.clear()` is called during `on_shutdown` to remove all subscriptions and cancel pending async tasks.


