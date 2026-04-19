# Probe Handoff — ClaudeUsageCard puppet probe failing

**Date:** 2026-04-19  
**Branch:** fix/issue-163-main-profile  
**Symptom:** Profile Manager widget shows burn rate / usage columns as empty (`null`) for all profiles.

---

## What the probe does

`ClaudeUsageCard._puppet_probe()` spawns a PTY session running:

```
docker exec -it supreme-claudemander-util env CLAUDE_CONFIG_DIR=/profiles/<name> claude --dangerously-skip-permissions
```

It feeds the scrollback through a `pyte` virtual screen, handles the trust-folder and bypass-permissions dialogs, sends `/usage`, and parses the percentage output.

---

## Observed failure

Every probe session dies within ~300ms of starting with:

```
ServiceCard claude-usage/hongy: stopped
...
SessionManager: all sessions detached
Session rts-XXXX: read loop ended
ClaudeUsageCard hongy: session died unexpectedly
```

This is immediately followed in the log by:

```
OSError: [Errno 98] error while attempting to bind on address ('127.0.0.1', 3000): address already in use
```

That OSError is from a **second `python -m claude_rts` process** that starts ~300ms after the first, tries to bind port 3000, fails, and runs its own `on_shutdown` — which calls `stop_all` on its own CardRegistry. The interleaved logs make it look like the first server's cards are being stopped, but it is actually the second server's shutdown sequence.

The **first server keeps running** but its probe sessions have already been destroyed by its own orphan-cleanup triggered by the second server's shutdown event (or possibly the sessions detect the container is busy and exit).

---

## Root cause: something spawns a second server instance

The second server is spawned reliably ~300ms after the first starts. It was observed:

- With and without `--no-browser`  
- From the `/proc` cmdline dump, a VS Code remote-containers node process (`vscode-remote-containers-server-*.js`) is the parent of `python -m claude_rts --dev-config start-claude`

**Hypothesis:** VS Code's `devcontainer.json` `postStartCommand` or a VS Code task / launch configuration is auto-spawning the server in response to port 3000 becoming active. The `"onAutoForward": "openBrowser"` in `devcontainer.json` is a candidate — it may trigger a browser or extension action that re-invokes the server.

The exact spawner was **not yet identified**. A clean repro is:

```bash
# Kill everything first
pkill -f claude_rts; fuser -k 3000/tcp; sleep 2

# Start server, note PID
python -m claude_rts --dev-config start-claude --no-browser &
echo "PID=$!"

# Wait ~1s then list all python processes
sleep 2 && cat /proc/*/cmdline 2>/dev/null | tr '\0' ' ' | grep claude_rts
```

If two PIDs appear, the duplicate spawner is active.

---

## What we confirmed works

The probe command itself is correct. When run directly via ptyprocess (simulating what the server does):

```python
import ptyprocess
cmd = ['docker', 'exec', '-it', 'supreme-claudemander-util',
       'env', 'CLAUDE_CONFIG_DIR=/profiles/hongy',
       'claude', '--dangerously-skip-permissions']
p = ptyprocess.PtyProcessUnicode.spawn(cmd, dimensions=(50, 220))
```

Claude starts successfully and shows the trust-folder dialog:

```
Accessing workspace: /home/util
Quick safety check: Is this a project you created or one you trust?
  1. Yes, I trust this folder
  2. No, exit
```

So the PTY + docker exec path is functional. The probe would work if allowed to run to completion.

---

## What was tried and ruled out

| Attempt | Result |
|---|---|
| Changed `docker exec -it` → `-i` | Claude detects no TTY inside container, exits: `"Input must be provided either through stdin or as a prompt argument when using --print"` |
| `--no-browser` flag | Second server still spawns |
| Multiple clean restarts | Consistent — second server always appears within ~300ms |

---

## Next steps

1. **Identify the spawner.** Check VS Code tasks (`.vscode/tasks.json`), launch configs (`.vscode/launch.json`), and any Claude Code extension hooks that might watch port 3000 and re-invoke the server. Also check `postStartCommand` — it runs `sudo -n chmod 666 /var/run/docker.sock` but nothing else; verify this isn't silently doing more.

2. **Confirm the second-server hypothesis** by capturing the PPID of both processes and tracing to the common ancestor.

3. **Guard probe sessions against external `stop_all`.** As a defensive fix, `_puppet_probe` could detect that the stop was externally triggered (not a session EOF) and reschedule rather than treating it as a failure.

4. **Re-test probing** after resolving the duplicate-server issue — the probe machinery itself is correct and should work once sessions aren't being torn down prematurely.

---

## Relevant files

| File | Role |
|---|---|
| `claude_rts/cards/claude_usage_card.py:86` | `probe_command()` — builds the docker exec command |
| `claude_rts/cards/claude_usage_card.py:140` | `_puppet_probe()` — full probe loop with pyte screen |
| `claude_rts/cards/service_card.py` | `stop()` — what kills the probe mid-run |
| `claude_rts/__main__.py:138` | `open_browser` / `launch_electron` startup hooks |
| `.devcontainer/devcontainer.json` | `postStartCommand`, `onAutoForward` — possible spawner config |
