const { app, BrowserWindow, Menu } = require("electron");
const path = require("path");

// Parse CLI arguments
const args = process.argv.slice(2);
const isDev = args.includes("--dev");
const portArgIndex = args.indexOf("--port");
const port =
  portArgIndex !== -1 && args[portArgIndex + 1]
    ? parseInt(args[portArgIndex + 1], 10)
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
      enableWebSQL: false,
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
      // Allow F11 for fullscreen toggle
      if (input.key === "F11") {
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

  // --- Block context menu ---
  mainWindow.webContents.on("context-menu", (event) => {
    event.preventDefault();
  });

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
