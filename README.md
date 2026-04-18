# supreme-claudemander

An RTS-style terminal canvas for managing devcontainer hubs. Terminal shells live as draggable, resizable cards on a pannable/zoomable 4K canvas — like commanding units in an RTS game.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**Primary platform: Linux and macOS.** Windows is community-supported (see below).

## What it does

- Opens a browser with a 4K canvas showing all your running devcontainers
- Each container gets a terminal card you can drag, resize, and interact with
- Zoom in to work in a terminal, zoom out to see them all at once
- Minimap for quick navigation
- Right-click to spawn additional terminal sessions
- Full PTY support: colors, readline, tab completion, Ctrl+C

## Screenshot

```
┌──────────────────────────────────────────────────────────────┐
│ supreme-claudemander   6 hub(s) | 6 terminal(s)   42%     [Fit all] ⚙│
├──────────────────────────────────────────────────────────────┤
│ ┌─[A hub_1]──────┐ ┌─[B hub_2]──────┐ ┌─[C hub_3]──────┐  │
│ │ vscode@hub_1:$ │ │ vscode@hub_2:$ │ │ vscode@hub_3:$ │  │
│ │ ls             │ │ npm test       │ │ cmake --build  │  │
│ │ > src/ tests/  │ │ ✓ 42 passing   │ │ [100%] Built   │  │
│ └────────────────┘ └────────────────┘ └────────────────┘  │
│ ┌─[D hub_4]──────┐ ┌─[E hub_5]──────┐ ┌─[F hub_6]──────┐  │
│ │ vscode@hub_4:$ │ │ vscode@hub_5:$ │ │ vscode@hub_6:$ │  │
│ └────────────────┘ └────────────────┘ └────────────────┘  │
│ ┌────┐                                                      │
│ │mini│  (minimap with viewport rect + hub symbols)          │
│ └────┘                                                      │
└──────────────────────────────────────────────────────────────┘
```

## Quick start

### Devcontainer (recommended)

Open this repository in VS Code and choose **Reopen in Container**. The devcontainer installs all dependencies and starts the server automatically. This is the easiest path on any platform.

### Linux / macOS (native)

Requirements:

- Python 3.10+
- Docker running (containers accessible via `docker` CLI)

Install and run:

```bash
pip install supreme-claudemander
python -m claude_rts
```

The server starts on `http://localhost:3000` and opens your browser automatically. Press `Ctrl+C` to stop — your containers keep running.

### Windows (community supported)

```bash
pip install "supreme-claudemander[windows]"
python -m claude_rts
```

> **Note**: The `[windows]` extra installs `pywinpty` as a placeholder for future community contribution. PTY-backed features (terminal cards, util-container PTY commands) do not currently work on Windows — `pty_compat.py` uses `ptyprocess` which is POSIX-only. The server will start, but spawning terminal cards will fail. For a fully working experience on a Windows machine, use the [devcontainer](#devcontainer-recommended) path. See [issue #157](https://github.com/hyang0129/supreme-claudemander/issues/157) to contribute a Windows PTY branch.

### Build from source

```bash
git clone https://github.com/hyang0129/supreme-claudemander.git
cd supreme-claudemander
pip install -e .
python -m claude_rts
```

## Architecture

```
Browser (localhost:3000)
  └─ 4K canvas with pan/zoom
       ├─ Terminal cards (xterm.js) ─── WebSocket ───┐
       ├─ Minimap                                    │
       └─ Context menu                               │
                                                     │
Python server (aiohttp) — runs on the HOST           │
  ├─ GET  /           → index.html                   │
  ├─ GET  /api/hubs   → discover containers          │
  └─ WS   /ws/{hub} ←───────────────────────────────┘
       └─ PTY → docker exec -it ...
```

**Server-on-host**: The Python server runs on your host machine (not inside a container) and communicates with Docker containers via the Docker socket. This lets it manage container lifecycles and forward PTY sessions without needing privileged access inside a container.

**Backend**: Python aiohttp server. On Linux/macOS, PTYs use the standard `pty` module. On Windows, pywinpty provides ConPTY terminal bridging.

**Frontend**: Single `index.html` file. xterm.js for terminal rendering. No build step, no npm — external libs load from CDN.

**Container discovery**: Queries `docker ps` for containers with the `devcontainer.local_folder` label.

## Options

```
python -m claude_rts --port 4000          # custom port
python -m claude_rts --no-browser         # don't auto-open browser
python -m claude_rts --electron           # launch in Electron shell
python -m claude_rts --config-dir PATH    # use a custom config directory (default: ~/.supreme-claudemander)
python -m claude_rts --dev-config         # isolated dev config (wiped on each start)
```

### `--config-dir`

By default, config and canvas layouts are stored in `~/.supreme-claudemander/`. Pass `--config-dir` to point to a different directory — useful for running multiple isolated instances or for CI environments:

```bash
python -m claude_rts --config-dir /tmp/my-test-config
```

#### Electron shell (optional)

The `--electron` flag requires Node.js and the repo's `electron/` dependencies:

```bash
cd electron && npm install && cd ..
python -m claude_rts --electron
```

## Controls

### Canvas

| Action | Effect |
|--------|--------|
| Scroll wheel | Zoom in/out (cursor-centered) |
| Drag empty space | Pan the canvas |
| Double-click empty space | Fit all cards in view |
| Right-click empty space | Spawn a new terminal |
| Ctrl+0 | Fit all |

### Terminal cards

| Action | Effect |
|--------|--------|
| Drag title bar | Move card |
| Drag bottom-right corner | Resize card |
| Double-click anywhere on card | Zoom to fill |
| Click card | Focus / bring to front |
| X button | Close terminal |
| Ctrl+Shift+C | Copy selection |
| Ctrl+Shift+V | Paste |
| Escape | Deselect |

### Status indicators

Each terminal card shows a hub symbol (A, B, C...) colored by state:

| Color | Meaning |
|-------|---------|
| Green | Working (recent output) |
| Yellow | Idle (waiting for input) |
| Red | Dead / disconnected |

### Minimap

The minimap in the top-left corner shows all card positions as colored symbols. Click anywhere on it to jump to that location.

## Settings

Click the gear icon in the status bar to configure:

- **Copy shortcut**: Ctrl+Shift+C, Ctrl+C when selected, or auto-copy on select
- **Paste shortcut**: Ctrl+Shift+V, Ctrl+V, or both
- **Right-click on terminal**: paste or do nothing
- **Idle threshold**: seconds before a terminal is marked idle (3/5/10/30)

Settings are saved to localStorage.

## Configuration

Config is stored in `~/.supreme-claudemander/config.json` (or the directory specified by `--config-dir`):

```json
{
  "startup_script": "discover-devcontainers",
  "sessions": { "orphan_timeout": 300, "scrollback_size": 65536 },
  "util_container": { "name": "supreme-claudemander-util", "mounts": {} }
}
```

Canvas layouts are stored in `~/.supreme-claudemander/canvases/{name}.json`.

## Development

### Running tests

```bash
pip install -e ".[test]"
pytest tests/ -v
```

### Project structure

```
claude_rts/
  __init__.py
  __main__.py          # CLI entry point
  server.py            # aiohttp routes, WebSocket handlers, widget endpoints
  sessions.py          # SessionManager, ScrollbackBuffer — PTY persistence
  discovery.py         # docker container discovery
  config.py            # file-based config and canvas layout persistence
  startup.py           # pluggable startup scripts
  util_container.py    # supreme-claudemander-util container management
  profile_manager.py   # CredentialManager, burn rate, usage cache
  Dockerfile.util      # utility container image (Python + Node.js + claude CLI)
  static/
    index.html         # entire frontend (inline JS/CSS, ~2400 lines)
tests/
  test_discovery.py         # hub discovery parsing
  test_server.py            # HTTP + WebSocket endpoints
  test_main.py              # CLI argument handling
  test_config.py            # config and canvas CRUD
  test_startup.py           # startup scripts
  test_sessions.py          # PTY session management
  test_server_credentials.py # credential endpoints
  test_profile_manager.py   # burn rate and usage cache
```

### Logging

Server operations are logged via loguru to stderr (colored) and `supreme-claudemander.log` (rotating, 10 MB). Terminal I/O is intentionally not logged.

## License

MIT
