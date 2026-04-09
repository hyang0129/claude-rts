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

- **pywinpty for ConPTY**: `asyncio.create_subprocess_exec` only gives pipes (no PTY), so `docker.exe exec -it` fails or has no echo. pywinpty provides a real Windows ConPTY.
- **Session persistence**: SessionManager decouples PTY lifetime from WebSocket. PTYs run in server memory with a 64KB scrollback ring buffer. Orphan reaper cleans up after 5 min.
- **Single HTML file**: All JS/CSS is inline in `index.html`. External libs (xterm.js) load from CDN. No npm, no bundler.
- **Card class hierarchy**: `Card` base → `TerminalCard`, `WidgetCard`, `LoaderCard`. Enables mixed dashboards.
- **No container lifecycle management**: supreme-claudemander only attaches to running containers. Starting/stopping is out of scope.
- **docker.exe not docker**: On Windows, `docker` (no extension) is a shell script that `CreateProcessW` can't execute. Always use `docker.exe`.

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
| `test_terminal_card.py` | 11 | TerminalCard lifecycle, CardRegistry, server integration |
| `test_claude_api.py` | 30 | Claude terminal control API (CRUD, send/read, strip_ansi, /ws/control, full lifecycle integration) |
| `test_event_bus.py` | 14 | EventBus core (subscribe, emit, unsubscribe, wildcard, async, errors, clear) + integration (ServiceCard bus emit, CardRegistry events) |
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
| POST | `/api/claude/terminal/create` | Create a TerminalCard + PTY session (params: cmd, hub, container, cols, rows, x, y, w, h) |
| POST | `/api/claude/terminal/{id}/send` | Write text to a terminal PTY |
| GET | `/api/claude/terminal/{id}/read` | Read scrollback (optional: strip_ansi, last_n) |
| GET | `/api/claude/terminal/{id}/status` | Session metadata (alive, age, idle, cmd) |
| DELETE | `/api/claude/terminal/{id}` | Stop card, clean up session and card registry |
| GET | `/api/claude/terminals` | List all terminal cards with metadata |
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


