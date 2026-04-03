# supreme-claudemander

An RTS-style terminal canvas for managing devcontainer hubs. Terminal shells live as draggable, resizable cards on a pannable/zoomable 4K canvas — like commanding units in an RTS game.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

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

### Prerequisites

- Windows 10/11 with Docker Desktop running
- Python 3.10+
- Running devcontainers (with `devcontainer.local_folder` label)

### Install and run

```bash
git clone https://github.com/hyang0129/supreme-claudemander.git
cd supreme-claudemander
pip install -e .
python -m claude_rts
```

The server starts on `http://localhost:3000` and opens your browser automatically. Press `Ctrl+C` to stop — your containers keep running.

### Options

```
python -m claude_rts --port 4000       # custom port
python -m claude_rts --no-browser      # don't auto-open browser
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

## Architecture

```
Browser (localhost:3000)
  └─ 4K canvas with pan/zoom
       ├─ Terminal cards (xterm.js) ─── WebSocket ───┐
       ├─ Minimap                                    │
       └─ Context menu                               │
                                                     │
Python server (aiohttp)                              │
  ├─ GET  /           → index.html                   │
  ├─ GET  /api/hubs   → discover containers          │
  └─ WS   /ws/{hub} ←───────────────────────────────┘
       └─ pywinpty ConPTY → docker.exe exec -it ...
```

**Backend**: Python aiohttp server with pywinpty for Windows ConPTY terminal bridging.

**Frontend**: Single `index.html` file. xterm.js for terminal rendering. No build step, no npm — external libs load from CDN.

**Container discovery**: Queries `docker.exe ps` for containers with the `devcontainer.local_folder` label.

## Settings

Click the gear icon in the status bar to configure:

- **Copy shortcut**: Ctrl+Shift+C, Ctrl+C when selected, or auto-copy on select
- **Paste shortcut**: Ctrl+Shift+V, Ctrl+V, or both
- **Right-click on terminal**: paste or do nothing
- **Idle threshold**: seconds before a terminal is marked idle (3/5/10/30)

Settings are saved to localStorage.

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
  __main__.py       # CLI entry point
  server.py         # aiohttp routes + WebSocket-to-PTY bridge
  discovery.py      # docker container discovery
  static/
    index.html      # entire frontend (inline JS/CSS)
tests/
  test_discovery.py # hub discovery parsing
  test_server.py    # HTTP + WebSocket endpoints
  test_main.py      # CLI argument handling
```

### Logging

Server operations are logged via loguru to stderr (colored) and `supreme-claudemander.log` (rotating, 10 MB). Terminal I/O is intentionally not logged.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for detailed milestones. M0–M4 are complete.

## License

MIT
