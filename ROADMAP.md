# claude-rts — RTS-Style Terminal Canvas

A web-based terminal multiplexer where devcontainer shells live as draggable, resizable cards on a pannable/zoomable 4K canvas — like commanding units in an RTS game.

## MVP Assumptions

1. Claude does all configuration and setup — the user should not need to edit config files
2. User launches with a single script; browser auto-opens to the server; server dies when the script is terminated (Ctrl+C)
3. Minimap in the top-left corner showing current viewport position + markers for each terminal card
4. Canvas size defaults to 3840x2160 (4K)
5. Right-click on empty canvas opens a context menu with "Spawn terminal copy" — user picks a hub, new terminal card appears at the click position

## Architecture

```
Browser (localhost:3000)
  └─ 4K canvas (3840x2160, pan/zoom)                     [HTML/JS + xterm.js]
       ├─ Terminal card [hub_1] ─── WebSocket ───┐
       ├─ Terminal card [hub_2] ─── WebSocket ───┤
       ├─ Terminal card [hub_3] ─── WebSocket ───┤
       ├─ ...additional spawned copies           ┤
       │                                         │
       ├─ Minimap (top-left)                     │
       └─ Context menu (right-click)             │
                                                 │
Python server (aiohttp, localhost:3000)          │
  ├─ GET  /              → index.html            │
  ├─ GET  /api/hubs      → discovered hubs JSON  │
  └─ WS   /ws/{hub}  ←──────────────────────────┘
       └─ per connection:
            pywinpty ConPTY: docker.exe exec -it -u vscode <container> bash -l
            stdin:  browser keystrokes → process stdin
            stdout: process stdout → browser terminal
```

## Tech Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Terminal rendering | xterm.js + xterm-addon-fit (CDN) | Industry standard, same as VS Code terminal |
| WebSocket-to-PTY bridge | Python pywinpty (ConPTY) | Full PTY on Windows (colors, readline, resize) |
| Canvas pan/zoom | Custom pointer events + wheel | No library dependency, cursor-centered zoom |
| Drag/resize | Custom pointer events | Title bar drag, corner resize handles |
| Frontend | Single index.html, no build step | Served by the Python server |
| Backend | aiohttp | Async HTTP + WebSocket in one package |
| Container discovery | subprocess `docker.exe ps` | Simple, no SDK dependency |
| Logging | loguru | Verbose server ops, rotating file log |
| Launcher | `python -m claude_rts` (argparse) | Starts server, opens browser, Ctrl+C kills all |

## Milestones

### M0 — Single terminal in browser (plumbing) ✅
- [x] Project scaffolding: pyproject.toml, claude_rts package, static/index.html
- [x] aiohttp server serves index.html on GET /
- [x] WebSocket endpoint /ws/{hub} spawns docker exec via pywinpty ConPTY
- [x] Bridge PTY stdin/stdout over WebSocket binary frames
- [x] index.html: load xterm.js from CDN, connect to /ws/{hub}, render terminal
- [x] Handle PTY sizing: client sends JSON resize message, server calls pty.setwinsize
- [x] `python -m claude_rts` starts server on :3000 and opens browser
- [x] Ctrl+C cleanly kills PTY processes (containers keep running)
- [x] Copy/paste: Ctrl+Shift+C/V, right-click paste
- [x] Verbose loguru logging (stderr + rotating file)

### M1 — Multi-terminal canvas ✅
- [x] /api/hubs endpoint: discover running devcontainers via docker.exe ps
- [x] Frontend fetches /api/hubs on load, creates one terminal card per hub
- [x] Cards absolute-positioned on a 3840x2160 canvas div
- [x] Default layout: 3x2 grid centered on canvas
- [x] Each card: title bar (hub name) + xterm.js + own WebSocket + ConPTY
- [x] Cursor-centered zoom (scroll wheel), drag empty space to pan
- [x] Minimap (top-left, 200x112px): scaled canvas, dots per card, viewport rect, click to jump
- [x] Drag cards by title bar, resize by corner handle
- [x] xterm.js addon-fit re-fits on card resize, sends resize to PTY
- [x] Z-order: clicked card comes to front
- [x] Right-click empty canvas: context menu to spawn terminal copy at click position
- [x] Close button (X) on cards: kills PTY, removes card
- [x] Fit-all button in status bar

### M2 — Symbol coding and terminal state ([#1](https://github.com/hyang0129/claude-rts/issues/1))
- [ ] Assign each hub a unique symbol (A, B, C... or hub number)
- [ ] Replace colored dot in title bar with hub symbol
- [ ] Symbol color reflects terminal state, not hub identity:
  - **Green** = doing work (stdout activity in last N seconds)
  - **Yellow** = idle / needs user input (no recent output, process alive)
  - **Red** = dead terminal / major issue (WebSocket closed, process exited)
- [ ] Minimap renders hub symbols instead of colored dots, colored by state
- [ ] State detection: track last stdout timestamp per card, periodic check
- **Exit criteria**: Can identify hubs by symbol and terminal health by color at a glance

### M3 — Polish
- [ ] Auto-reconnect on WebSocket drop (exponential backoff)
- [ ] Double-click title bar to zoom-to-fill (card takes full viewport)
- [ ] Keyboard shortcuts: Ctrl+0 zoom-to-fit, Escape to deselect
- [ ] Save card positions/sizes to localStorage, restore on reload
- **Exit criteria**: Feels like a real tool

### M4 — Settings menu
- [ ] Accessible settings panel (gear icon in status bar, or keyboard shortcut)
- [ ] Copy/paste configuration:
  - [ ] Choose copy shortcut (Ctrl+Shift+C, Ctrl+C when selection exists, or auto-copy on select)
  - [ ] Choose paste shortcut (Ctrl+Shift+V, Ctrl+V, right-click, or all)
  - [ ] Toggle right-click behavior (paste vs context menu)
- [ ] Settings persisted to localStorage
- [ ] Settings applied immediately (no reload required)
- **Exit criteria**: User can configure copy/paste to match their preferred workflow

## Resolved Questions

1. **PTY on Windows** — Resolved: pywinpty provides ConPTY. `asyncio.create_subprocess_exec` only gives pipes (no echo, no colors). pywinpty + `docker.exe exec -it` gives full terminal support. The PTY read loop runs in a thread executor.

2. **docker vs docker.exe** — Resolved: On Windows, `docker` (no extension) is a shell script that `CreateProcessW` can't execute. Always use `docker.exe` explicitly.

## Open Questions

1. **Container lifecycle** — MVP is attach-only. Starting/stopping containers is out of scope.

2. **Canvas size** — Fixed 4K for MVP. Could make configurable or auto-scale later.

3. **State detection heuristics** — How long after last stdout before marking "idle"? Need to tune the threshold (M2).

## File Structure

```
claude-rts/
  ROADMAP.md
  CLAUDE.md
  pyproject.toml
  claude_rts/
    __init__.py
    __main__.py          # CLI: parse args, loguru setup, start server, open browser
    server.py            # aiohttp app: static files, /api/hubs, /ws/{hub}, pywinpty bridge
    discovery.py         # docker.exe ps parsing → list of {hub, container}
    static/
      index.html         # single-page canvas UI (all JS/CSS inline)
```
