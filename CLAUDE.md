# supreme-claudemander

RTS-style terminal canvas — a web-based multiplexer where devcontainer shells live as draggable, resizable cards on a pannable/zoomable 4K canvas.

## Server Rule

**Never run more than one server instance at a time.** Before starting a server, kill any existing one:

```bash
netstat -ano | grep "LISTENING" | grep -E ":300[0-9] " | awk '{print $NF}' | sort -u | while read pid; do taskkill //F //PID $pid 2>/dev/null; done
```

Verify the port is free before starting. Running multiple instances causes port conflicts, confusing log output, and stale probe sessions that are hard to attribute to the right run.

## Quick Start

```bash
cd d:/containers/supreme-claudemander
pip install -e .
python -m claude_rts                          # starts server on :3000, opens browser
python -m claude_rts --port 3001 --no-browser # custom port, no auto-open
python -m claude_rts --test-mode              # enable test puppeting API
```

## Architecture

- **Backend**: Python (aiohttp) serving a single HTML page, REST APIs, and WebSocket endpoints
- **Frontend**: Single `index.html` with xterm.js (all via CDN, no build step)
- **Terminal bridge**: WebSocket → SessionManager → pywinpty ConPTY → docker exec
- **Session persistence**: PTY sessions survive browser refresh; scrollback ring buffer for replay
- **Container discovery**: `docker.exe ps` with devcontainer label filtering
- **Utility container**: Lightweight Linux container for ops tasks (claude-usage probing, etc.)

## File Structure

```
claude_rts/
  __init__.py
  __main__.py          # CLI: argparse, loguru setup, start server + open browser
  server.py            # aiohttp routes, WebSocket handlers, session endpoints, test API
  sessions.py          # SessionManager, ScrollbackBuffer, Session — PTY persistence
  discovery.py         # docker.exe ps → list of {hub, container}
  config.py            # File-based config and canvas layout persistence (~/.supreme-claudemander/)
  startup.py           # Pluggable startup scripts (discover-devcontainers, custom)
  util_container.py    # Manage the supreme-claudemander-util container (build, start, exec)
  Dockerfile.util      # Utility container image (Python + Node.js + claude CLI)
  probe_loop.sh        # Background usage probe script for utility container
  static/
    index.html         # Entire frontend (JS/CSS inline, ~2400 lines)
```

## Key Design Decisions

- **pywinpty for ConPTY**: `asyncio.create_subprocess_exec` only gives pipes (no PTY), so `docker.exe exec -it` fails or has no echo. pywinpty provides a real Windows ConPTY.
- **Session persistence**: SessionManager decouples PTY lifetime from WebSocket. PTYs run in server memory with a 64KB scrollback ring buffer. Orphan reaper cleans up after 5 min.
- **Single HTML file**: All JS/CSS is inline in index.html. External libs (xterm.js) load from CDN. No npm, no bundler.
- **Card class hierarchy**: Card base class → TerminalCard, WidgetCard, LoaderCard. Enables mixed dashboards.
- **No container lifecycle management**: supreme-claudemander only attaches to running containers. Starting/stopping containers is out of scope.

## Development

### Dependencies

- `aiohttp` — async HTTP + WebSocket server
- `pywinpty` — Windows ConPTY bindings (PTY for docker exec)
- `loguru` — structured logging

### Testing

```bash
# Run all tests (72 tests)
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_sessions.py -v

# Run with test mode server (enables puppeting API)
python -m claude_rts --test-mode
# Or via env var:
CLAUDE_RTS_TEST_MODE=1 python -m claude_rts
```

#### Test structure

| File | Tests | What it covers |
|------|-------|----------------|
| `test_discovery.py` | 6 | Docker hub discovery parsing |
| `test_server.py` | 9 | HTTP routes, widget endpoints, route registration |
| `test_main.py` | 4 | CLI argument handling |
| `test_config.py` | 23 | Config CRUD, canvas CRUD, API endpoints |
| `test_startup.py` | 7 | Startup scripts, API endpoint |
| `test_util_container.py` | 6 | Claude-usage widget API, util container status |
| `test_sessions.py` | 19 | ScrollbackBuffer, SessionManager, test puppeting API |

#### Test puppeting API

When `--test-mode` is enabled, these HTTP endpoints allow automated terminal control without a browser:

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/test/session/create?cmd=...` | Create a session, returns session_id |
| POST | `/api/test/session/{id}/send` | Send text to PTY stdin |
| GET | `/api/test/session/{id}/read` | Read scrollback output |
| GET | `/api/test/session/{id}/status` | Check session alive, client count |
| DELETE | `/api/test/session/{id}` | Kill session |
| GET | `/api/test/sessions` | List all sessions |

Tests use `MockPty` (in `test_sessions.py`) to avoid needing Docker. For integration tests against real containers, use `--test-mode` with a running container.

### Logging

Server ops are logged verbosely via loguru:
- stderr: colored, human-readable
- `supreme-claudemander.log`: rotating file log (10 MB, 3 day retention)

Terminal data (stdout bytes) is intentionally NOT logged.

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serves `static/index.html` |
| GET | `/api/hubs` | Discovered devcontainers |
| GET | `/api/startup` | Run startup script, return card descriptors |
| GET/PUT | `/api/config` | Read/write config |
| GET/PUT/DELETE | `/api/canvases/{name}` | Canvas layout CRUD |
| GET | `/api/sessions` | List active sessions |
| GET | `/api/widgets/system-info` | System info widget data |
| GET | `/api/widgets/claude-usage` | Claude usage per profile |
| WS | `/ws/session/new?cmd=...` | Create persistent session |
| WS | `/ws/session/{session_id}` | Reconnect to existing session |
| WS | `/ws/exec?cmd=...` | Legacy: one-shot exec (no persistence) |
| WS | `/ws/{hub}` | Legacy: hub-based terminal |

### WebSocket Protocol

- **Binary frames** (browser → server): terminal keystrokes (UTF-8 encoded)
- **Binary frames** (server → browser): terminal output (or scrollback replay)
- **Text frames** (server → browser): JSON control messages: `{"session_id": "..."}`, `{"error": "..."}`, `{"type": "session_attached"}`
- **Text frames** (browser → server): JSON control messages: `{"type": "resize", "cols": 120, "rows": 40}`

### Config

Stored in `~/.supreme-claudemander/config.json`. Key fields:

```json
{
  "startup_script": "discover-devcontainers",
  "sessions": { "orphan_timeout": 300, "scrollback_size": 65536 },
  "util_container": { "name": "supreme-claudemander-util", "mounts": {} }
}
```

Canvas layouts stored in `~/.supreme-claudemander/canvases/{name}.json`.

## Usage Probe

**The `credential-manager` WidgetCard is the sole probe initiator.** There is no backend probe. See [docs/feature-testing-guide.md](docs/feature-testing-guide.md) (Probing Rule section) and [docs/cards-and-canvas.md](docs/cards-and-canvas.md) (Probe Debug Canvas section).

## Roadmap

See [ROADMAP.md](ROADMAP.md) for milestones. M0–M5 complete. Open issues tracked on GitHub.
