# supreme-claudemander

RTS-style terminal canvas — devcontainer shells as draggable, resizable cards on a pannable/zoomable 4K canvas.

## Server Rule

**Never run more than one server instance at a time.** Before starting a server, kill any existing one:

```bash
netstat -ano | grep "LISTENING" | grep -E ":300[0-9] " | awk '{print $NF}' | sort -u | while read pid; do taskkill //F //PID $pid 2>/dev/null; done
```

Verify the port is free before starting. Running multiple instances causes port conflicts, confusing log output, and stale probe sessions.

## Key Design Decisions

- **pywinpty for ConPTY**: `asyncio.create_subprocess_exec` only gives pipes (no PTY), so `docker.exe exec -it` fails or has no echo. pywinpty provides a real Windows ConPTY.
- **Session persistence**: SessionManager decouples PTY lifetime from WebSocket. PTYs run in server memory with a 64KB scrollback ring buffer. Orphan reaper cleans up after 5 min.
- **Single HTML file**: All JS/CSS is inline in `index.html`. External libs (xterm.js) load from CDN. No npm, no bundler.
- **Card class hierarchy**: `Card` base → `TerminalCard`, `WidgetCard`, `LoaderCard`. Enables mixed dashboards.
- **No container lifecycle management**: supreme-claudemander only attaches to running containers. Starting/stopping is out of scope.
- **docker.exe not docker**: On Windows, `docker` (no extension) is a shell script that `CreateProcessW` can't execute. Always use `docker.exe`.

## Testing

```bash
python -m pytest tests/ -v
CLAUDE_RTS_TEST_MODE=1 python -m claude_rts   # enables puppeting API at /api/test/...
```

| File | Tests | What it covers |
|------|-------|----------------|
| `test_discovery.py` | 6 | Docker hub discovery parsing |
| `test_server.py` | 7 | HTTP routes, widget endpoints, route registration |
| `test_main.py` | 4 | CLI argument handling |
| `test_config.py` | 23 | Config CRUD, canvas CRUD, API endpoints |
| `test_startup.py` | 7 | Startup scripts, API endpoint |
| `test_sessions.py` | 30 | ScrollbackBuffer, SessionManager, test puppeting API |

Tests use `MockPty` to avoid needing Docker.

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
| WS | `/ws/session/new?cmd=...` | Create persistent PTY session |
| WS | `/ws/session/{session_id}` | Reconnect to existing session |

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

