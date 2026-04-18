# supreme-claudemander

RTS-style terminal canvas — devcontainer shells as draggable, resizable cards on a pannable/zoomable 4K canvas.

## Server Rule

**Never run more than one server instance at a time.** Before starting a server, kill any existing one:

```bash
netstat -ano | grep "LISTENING" | grep -E ":300[0-9] " | awk '{print $NF}' | sort -u | while read pid; do taskkill //F //PID $pid 2>/dev/null; done
```

Verify the port is free before starting. Running multiple instances causes port conflicts, confusing log output, and stale probe sessions.

**Use `--electron` for manual/QA testing.** Launch via `python -m claude_rts --electron` to run in the Electron shell instead of a browser tab.

## Key Design Decisions

- **ptyprocess for POSIX PTY**: `ptyprocess` is the PTY backend on Linux/macOS. The Windows ConPTY branch (`pywinpty`) was removed — Windows is community-supported best-effort only.
- **Session persistence**: SessionManager decouples PTY lifetime from WebSocket. PTYs run in server memory with a 64KB scrollback ring buffer. Orphan reaper cleans up after 5 min.
- **Single HTML file**: All JS/CSS is inline in `index.html`. External libs (xterm.js) load from CDN. No npm, no bundler.
- **Card class hierarchy**: `Card` base → `TerminalCard`, `WidgetCard`, `LoaderCard`. Enables mixed dashboards.
- **Limited container lifecycle (start/stop only)**: supreme-claudemander can start and stop Docker containers via the VM Manager card. Creating, removing, and image management remain out of scope.
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
| `start-claude` | Start Claude button QA — priority_profile pre-set, Profile Manager widget on canvas |
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
| `test_claude_api.py` | 38 | Claude terminal control API (CRUD, send/read, strip_ansi, /ws/control, full lifecycle, ${priority_credential}, rename, recovery-script, card_updated broadcast) |
| `test_event_bus.py` | 14 | EventBus core (subscribe, emit, unsubscribe, wildcard, async, errors, clear) + integration (ServiceCard bus emit, CardRegistry events) |
| `test_vm_manager.py` | 18 | VM Manager API (discover containers, favorites CRUD, start/stop container, per-container actions, route registration) |
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
| GET/PUT | `/api/profiles/priority` | Read/set priority profile |
| GET | `/api/vms/discover` | Discover all Docker containers (running + stopped) with status |
| GET/PUT | `/api/vms/favorites` | Read/write VM Manager favorites list |
| POST | `/api/vms/{name}/start` | Start a stopped Docker container |
| POST | `/api/vms/{name}/stop` | Stop a running Docker container (optional `?timeout=N`) |
| PUT | `/api/vms/favorites/{name}/actions` | Update actions for a specific favorite container |
| POST | `/api/claude/terminal/create` | Create a TerminalCard + PTY session (params: cmd, hub, container, cols, rows, x, y, w, h) |
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

## VM Manager Action Schema

Each favorite container has an `actions` array of blueprint-action objects. Actions spawn blueprints scoped to the container:

```json
{
  "label": "Dev Shell",           // Required: button label
  "blueprint": "dev-shell",       // Required: name of a saved blueprint
  "context": { "branch": "main" } // Optional: extra variables merged with auto-injected container name
}
```

When an action button is clicked, the frontend calls `POST /api/blueprints/spawn` with `{name: act.blueprint, context: {container: "<fav-name>", ...act.context}}`. The container name is always injected automatically. New favorites default to an empty actions array.

`POST /api/claude/terminal/create` performs the same `${priority_credential}` substitution server-side on its `cmd` query parameter, so Canvas Claude (via the `open_terminal` MCP tool) can pass the placeholder through unchanged. If no `priority_profile` is configured, the placeholder is left as-is and a warning is logged.

## Canvas Claude MCP Tools

The Canvas Claude card (`claude_rts/mcp_server.py`) exposes a JSON-RPC stdio MCP server so Claude Code, running inside the card, can drive the canvas programmatically. Each tool is a thin wrapper around a REST endpoint.

| Tool | Wraps | Purpose |
|---|---|---|
| `open_terminal` | `POST /api/claude/terminal/create` | Spawn a new terminal card (supports `x, y, w, h, container, hub`). Server interpolates `${priority_credential}` in `cmd`. |
| `read_terminal` | `GET /api/claude/terminal/{id}/read` | Read scrollback (default: strip ANSI) |
| `write_terminal` | `POST /api/claude/terminal/{id}/send` | Write keystrokes to a terminal |
| `list_terminals` | `GET /api/claude/terminals` | List active terminal cards |
| `delete_terminal` | `DELETE /api/claude/terminal/{id}` | Close a terminal card |
| `rename_terminal` | `PUT /api/claude/terminal/{id}/rename` | Set a display name on a terminal card |
| `set_recovery_script` | `PUT /api/claude/terminal/{id}/recovery-script` | Set recovery script on a terminal card |
| `get_recovery_script` | `GET /api/claude/terminal/{id}/recovery-script` | Get recovery script for a terminal card |
| `vm_discover_containers` | `GET /api/vms/discover` | List all Docker containers (running + stopped) |
| `vm_get_favorites` | `GET /api/vms/favorites` | Read the VM Manager favorites list with blueprint-actions |
| `vm_get_container_actions` | `GET /api/vms/favorites` (filtered) | Return one favorite's `actions` array; errors on unknown container |
| `vm_set_container_actions` | `PUT /api/vms/favorites/{name}/actions` | Replace the full actions array for a favorite |
| `vm_append_container_action` | `GET` + `PUT` (atomic) | Append one blueprint-action without dropping existing entries |
| `vm_add_favorite` | `GET` + `PUT /api/vms/favorites` | Add a container to favorites (default: empty actions) |
| `vm_start_container` | `POST /api/vms/{name}/start` | Start a stopped container |
| `vm_stop_container` | `POST /api/vms/{name}/stop` | Stop a running container (optional `timeout`) |
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


