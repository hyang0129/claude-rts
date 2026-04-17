const { app, BrowserWindow, Menu } = require("electron");
const path = require("path");

// Parse CLI arguments
const args = process.argv.slice(2);
const isDev = args.includes("--dev");
const portArgIndex = args.indexOf("--port");
const parsedPort =
  portArgIndex !== -1 && args[portArgIndex + 1]
    ? parseInt(args[portArgIndex + 1], 10)
    : 3000;
const port =
  Number.isFinite(parsedPort) && parsedPort >= 1 && parsedPort <= 65535
    ? parsedPort
    : 3000;
const backendUrl = `http://localhost:${port}`;

let mainWindow = null;

function createWindow() {
  // Remove the default application menu entirely
  Menu.setApplicationMenu(null);

  mainWindow = new BrowserWindow({
    width: 1920,
    height: 1080,
    minWidth: 800,
    minHeight: 600,
    title: "Supreme Claudemander",
    show: false, // Show after ready-to-show to avoid flash
    backgroundColor: "#1a1a2e",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
      // Disable features not needed for an RTS
      spellcheck: false,
    },
  });

  // Start maximized
  mainWindow.maximize();

  // Show window once content is ready
  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  // --- Suppress all default Chromium accelerators ---
  // This fires before Chromium processes the keystroke, letting us block
  // browser-level shortcuts (Ctrl+W, Ctrl+R, Ctrl+T, Ctrl+L, F5, etc.)
  mainWindow.webContents.on("before-input-event", (_event, input) => {
    // Block browser-level keyboard shortcuts so the game receives all input.
    // We selectively allow Ctrl+Shift+I (DevTools) in dev mode.
    if (input.control || input.meta) {
      const key = input.key.toLowerCase();

      // Allow DevTools toggle in dev mode
      if (isDev && input.shift && key === "i") {
        return; // let it through
      }

      // Allow copy/paste/cut/select-all — these are useful even in an RTS
      if (["c", "v", "x", "a"].includes(key) && !input.shift && !input.alt) {
        return; // let it through
      }

      // Block everything else (Ctrl+W, Ctrl+R, Ctrl+T, Ctrl+L, Ctrl+N, etc.)
      _event.preventDefault();
    }

    // Block F-keys that have browser meanings
    if (["F5", "F7", "F11", "F12"].includes(input.key)) {
      if (isDev && input.key === "F12") {
        return; // allow DevTools in dev mode
      }
      // F11 toggles fullscreen — only on keyDown to avoid double-toggle
      if (input.key === "F11" && input.type === "keyDown") {
        mainWindow.setFullScreen(!mainWindow.isFullScreen());
        _event.preventDefault();
        return;
      }
      _event.preventDefault();
    }

    // Block Alt+Left, Alt+Right (navigation)
    if (
      input.alt &&
      (input.key === "ArrowLeft" || input.key === "ArrowRight")
    ) {
      _event.preventDefault();
    }
  });

  // Context menu is blocked by the preload script (contextmenu event handler).
  // No main-process handler needed — Electron's context-menu event on
  // webContents does not support preventDefault() for suppression.

  // --- Disable navigation ---
  // Prevent accidental navigation via links, drag-drop, or back/forward
  mainWindow.webContents.on("will-navigate", (event, _url) => {
    // Allow navigating to the backend URL (e.g. on reload)
    if (_url.startsWith(backendUrl)) {
      return;
    }
    event.preventDefault();
  });

  // Block new window creation (e.g. target="_blank" links)
  mainWindow.webContents.setWindowOpenHandler(() => {
    return { action: "deny" };
  });

  // Show a friendly error page if the backend is not reachable
  mainWindow.webContents.on("did-fail-load", (_event, errorCode, errorDesc) => {
    const errorHtml = `
      <html><body style="background:#1a1a2e;color:#e0e0e0;font-family:monospace;
        display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
        <div style="text-align:center;max-width:500px">
          <h1 style="color:#ff6b6b">Backend Unreachable</h1>
          <p>Could not connect to <code>${backendUrl}</code></p>
          <p style="color:#888">${errorDesc} (${errorCode})</p>
          <p style="margin-top:2em">Start the backend first:</p>
          <code style="background:#2a2a3e;padding:8px 16px;border-radius:4px">
            python -m claude_rts
          </code>
        </div>
      </body></html>`;
    mainWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(errorHtml)}`);
  });

  // Load the backend URL
  mainWindow.loadURL(backendUrl);

  // Open DevTools in dev mode
  if (isDev) {
    mainWindow.webContents.openDevTools({ mode: "detach" });
  }

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// --- App lifecycle ---

app.whenReady().then(() => {
  createWindow();

  app.on("activate", () => {
    // macOS: re-create window when dock icon is clicked and no windows exist
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  app.quit();
});

// Disable hardware acceleration if running in a VM or headless environment
// (uncomment if needed)
// app.disableHardwareAcceleration();
