# Cards and Canvases

## What is a Card?

A card is a draggable, resizable panel on the canvas. There are three card types:

| Class | Purpose |
|-------|---------|
| `TerminalCard` | An interactive terminal attached to a container via `docker exec` |
| `WidgetCard` | A read-only info panel (e.g. system info, Claude usage) |
| `LoaderCard` | A transient placeholder shown during startup discovery |

## What is a Canvas?

A canvas is a named layout of cards. It records the position, size, and identity of every card so the layout can be restored across server restarts.

Canvases are stored on disk at:

```
~/.supreme-claudemander/canvases/{name}.json
```

The active canvas on startup is controlled by `default_canvas` in `config.json` (default: `"main"`).

### Canvas JSON format

```json
{
  "name": "my-canvas",
  "canvas_size": [3840, 2160],
  "cards": [
    {
      "hub": "my-project",
      "container": "my-project_devcontainer_1",
      "x": 80,
      "y": 300,
      "w": 1720,
      "h": 960,
      "type": "terminal",
      "exec": "docker.exe exec -it my-project_devcontainer_1 bash",
      "session_id": "abc123"
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `hub` | Logical name for the terminal (matches the `name` from the startup card descriptor) |
| `container` | Docker container name or ID |
| `x`, `y` | Position on the 4K canvas (pixels) |
| `w`, `h` | Card dimensions (pixels) |
| `type` | `"terminal"` or `"widget"` |
| `exec` | Full command passed to the PTY (e.g. `docker.exe exec -it ...`) |
| `session_id` | Optional — reconnects to an existing PTY session on reload |

## How the Startup Script Populates Cards

On page load the frontend calls `GET /api/startup`. The backend runs the configured `startup_script` and returns a list of **card descriptors**. The frontend converts each descriptor into a `TerminalCard`.

```
config.json { startup_script: "qa-m6" }
    │
    ▼
startup.py: run_startup("qa-m6")
    │
    ▼
[{"type":"terminal","name":"rts-test-a","container":"rts-test-a","exec":"docker.exe exec -it rts-test-a bash"}, ...]
    │
    ▼
frontend: hubs = descriptors.map(...)  →  spawnCard() per hub  →  TerminalCards on canvas
```

If the startup script returns cards **and** a saved canvas layout exists for `default_canvas`, the frontend uses the saved positions/sizes but only for hubs that appear in both lists. New hubs not in the saved layout are placed at a random offset.

### Card descriptor format

```json
{
  "type": "terminal",
  "name": "rts-test-a",
  "container": "rts-test-a",
  "exec": "docker.exe exec -it rts-test-a bash"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `type` | yes | Always `"terminal"` for shell cards |
| `name` | yes | Unique label shown in the card header |
| `container` | no | Docker container name (informational) |
| `exec` | yes | Command the PTY will run |

## Built-in Startup Scripts

| Script name | Behaviour |
|-------------|-----------|
| `discover-devcontainers` | Runs `docker.exe ps` and returns one terminal per container that has devcontainer labels. Default for production use. |
| `qa-m6` | Returns two fixed terminals: `rts-test-a` (tmux installed) and `rts-test-b` (no tmux). Used for M6 persistence QA. Requires `qa/setup-containers.sh` to have been run first. |
| `from-layout` | Returns an **empty** list. The frontend then relies entirely on the saved canvas to restore cards. |
| `null` / omitted | Identical to `from-layout` — returns an empty list. |

Custom scripts can be placed in `~/.supreme-claudemander/startup/` as executable files that print a JSON array to stdout.

## Warning: `null` / `from-layout` vs. a Real Startup Script

`from-layout` and `null` return zero card descriptors. If no saved canvas exists (or the saved canvas references containers that are no longer running), the result is a blank canvas with no terminals.

**Use `from-layout` only when:**
- A saved canvas already exists and you want to restore it exactly as-is.

**Do not use `from-layout` / `null` when:**
- Setting up a QA or test environment from scratch.
- You need specific containers to appear on load.

For those cases, use a startup script that explicitly lists the terminals — such as `qa-m6` or a custom script in `~/.supreme-claudemander/startup/`.

---

## Probe Debug Canvas

The `probe-debug` canvas is the **canonical minimal environment for troubleshooting usage probe bugs**. It contains exactly one card — the `credential-manager` WidgetCard — and nothing else. This makes it the right starting point any time a probe is misbehaving: no terminal noise, no unrelated widgets, just the probe service card watching a single profile.

### When to use it

- Usage probe returns stale or incorrect data
- `claude-usage` output not parsing correctly
- Auto-probe not triggering (15-minute stale threshold)
- Burn rate calculations look wrong
- Any issue where you need to watch the probe cycle in isolation

### Canvas JSON

```json
{
  "name": "probe-debug",
  "canvas_size": [1920, 1080],
  "cards": [
    {
      "hub": "usage-monitor",
      "container": "",
      "x": 80,
      "y": 80,
      "w": 1760,
      "h": 900,
      "type": "widget",
      "widgetType": "credential-manager"
    }
  ]
}
```

Save this to `~/.supreme-claudemander/canvases/probe-debug.json` to make it available in the canvas switcher.

### Prerequisites

The utility container must auto-start with the server. Verify `~/.supreme-claudemander/config.json`:

```json
"util_container": { "auto_start": true, "mounts": { "~/.claude-profiles": "/profiles" } }
```

`auto_start` defaults to `true` in code — the only reason it would be `false` is a deliberate override. If the probe widget shows "Utility container not running", check this field first.

### Agent setup (headless)

Once #60 is implemented, agents can bootstrap this canvas without human interaction:

```
POST /api/qa/probe-debug-setup
→ { "canvas": "probe-debug", "profile": "<first-found>", "url": "/?canvas=probe-debug" }
```

This endpoint:
1. Calls `list_profiles()` (`util_container.py`) to find the first profile with `.credentials.json`
2. Writes the canvas JSON above to `~/.supreme-claudemander/canvases/probe-debug.json`
3. Returns the canvas name and the selected profile

If no profiles exist it returns `{"error": "no profiles found"}` rather than an empty canvas.

Until #60 lands, create the canvas manually: write the JSON above to disk, navigate to `/?canvas=probe-debug`, and the `credential-manager` widget will auto-probe on load.

### Relationship to QA scripts

Issue #58 (TC-1 through TC-8) maps directly onto what you see in this canvas:
- TC-1/TC-2: watch the widget's "last probed" timestamp update without a Refresh click
- TC-3: click the Probe button on a row and watch live data come back
- TC-4/TC-5: break `claude-usage` in the util container and observe the stale/error state in the widget rows
- TC-6: stop the util container and confirm the widget shows the container-down error

Related: #60 (headless setup endpoint), #58 (QA cases), #57 (probe implementation), #49 (credential-manager widget)
