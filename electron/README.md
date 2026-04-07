# Supreme Claudemander — Electron Shell

Standalone window for the Supreme Claudemander RTS canvas. Eliminates browser
hotkey conflicts (Ctrl+W, Ctrl+R, F5, etc.) so the game owns every input event.

## Prerequisites

- Node.js 18+ and npm
- The Python backend must be running (`python -m claude_rts`)

## Setup

```bash
cd electron
npm install
```

## Usage

Start the Python backend first, then launch the Electron shell:

```bash
# Terminal 1 — start the backend
python -m claude_rts

# Terminal 2 — launch the Electron window
cd electron
npm start
```

### Options

```bash
# Dev mode — opens DevTools, allows Ctrl+Shift+I and F12
npm run start:dev

# Custom port (if the backend is not on 3000)
npx electron . --port 3001
```

## What the shell does

- Loads `http://localhost:3000` in a maximized window (no browser chrome)
- Suppresses all Chromium keyboard shortcuts (Ctrl+W, Ctrl+R, Ctrl+T, F5, etc.)
- Blocks the right-click context menu
- Blocks middle-click auto-scroll
- Disables back/forward navigation
- Blocks drag-and-drop file navigation
- F11 toggles fullscreen

## What it does not do (yet)

- Auto-launch the Python backend (Phase 2)
- Package as a distributable .exe/.dmg/.AppImage (Phase 3)
- Custom title bar or system tray (Phase 4)
