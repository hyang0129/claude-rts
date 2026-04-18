# Feature Testing Guide

Goal: verify feature behaviour without human QA. Use the layered approach below — start at unit tests, reach for integration methods when unit coverage is insufficient.

---

## Layer 1 — Unit tests (pytest + MockPty)

The fastest and most reliable layer. No Docker, no browser required.

### MockPty pattern

`MockPty` (defined in `tests/test_sessions.py`) replaces `PtyProcess` so session logic can be tested without a real ConPTY:

```python
@pytest.fixture
def mgr(monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    return SessionManager()
```

Use `MockPty._output_queue` to feed fake PTY output and `MockPty._written` to assert what was sent to stdin.

### Mocking subprocess calls

For features that shell out (tmux probe, Docker commands), mock `asyncio.create_subprocess_exec`:

```python
async def mock_subprocess_exec(*args, **kwargs):
    mock_proc = AsyncMock()
    if "tmux" in args:
        mock_proc.communicate.return_value = (b"tmux 3.3a\n", b"")
        mock_proc.returncode = 0
    else:
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 1
    return mock_proc

monkeypatch.setattr("asyncio.create_subprocess_exec", mock_subprocess_exec)
```

### HTTP route tests (aiohttp_client)

Use `aiohttp_client` fixture for REST endpoint coverage:

```python
@pytest.fixture
async def client(aiohttp_client, monkeypatch):
    monkeypatch.setattr("claude_rts.sessions.PtyProcess", MockPty)
    return await aiohttp_client(create_app(test_mode=True))

async def test_my_endpoint(client):
    resp = await client.get("/api/my-endpoint")
    assert resp.status == 200
    data = await resp.json()
    assert data["field"] == "expected"
```

### What to assert at this layer

- Session state changes (`alive`, `tmux_backed`, `container`, scrollback contents)
- Command routing (which PTY command was spawned — check `spawned_cmds`)
- Input sanitization (container name validation, injection rejection)
- Cache behaviour (`_tmux_cache` populated, result reused on second call)
- API response shape and status codes

---

## Layer 2 — Test puppeting API (real server, no browser)

When a feature involves PTY I/O, timing, or real process output that MockPty cannot simulate, use the test puppeting API with a real server running `--test-mode`.

### Start the server

```bash
python -m claude_rts --port 3002 --test-mode --no-browser 2>&1 | tee /tmp/rts-test.log
```

### Puppeting endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/test/session/create?cmd=...` | Spawn a session, returns `{"session_id": "rts-..."}` |
| `POST` | `/api/test/session/{id}/send` | Write to PTY stdin (body = raw text) |
| `GET`  | `/api/test/session/{id}/read` | Read scrollback `{"output": "...", "size": N}` |
| `GET`  | `/api/test/session/{id}/status` | Check `{"alive": bool, "client_count": N}` |
| `DELETE` | `/api/test/session/{id}` | Kill session |
| `GET`  | `/api/test/sessions` | List all sessions |

### Verifying via WebFetch

Claude Code can call WebFetch to hit localhost silently (no browser window, no user visibility):

```
WebFetch http://localhost:3002/api/test/session/create  (POST)
WebFetch http://localhost:3002/api/test/session/rts-abc123/read
```

**Typical flow for a PTY feature:**

1. `POST /api/test/session/create?cmd=docker+exec+-it+rts-test-a+bash` → get `session_id`
2. `POST /api/test/session/{id}/send` body: `echo MARKER_OUTPUT\n`
3. Sleep 500ms (PTY round-trip)
4. `GET /api/test/session/{id}/read` → assert `"MARKER_OUTPUT"` in `output`
5. `DELETE /api/test/session/{id}`

### Verifying session metadata

```
GET /api/sessions
```

Returns `tmux_backed`, `client_count`, `scrollback_size`, `age_seconds`, `idle_seconds` for all live sessions. Use this to assert that a session is tmux-backed after creation.

---

## Layer 3 — Log inspection

The server logs cover every significant state transition. Grep logs to verify behaviour without needing a browser or WebSocket client.

### Key log patterns

| Feature | Log pattern to grep |
|---------|-------------------|
| tmux probe fired | `tmux available in container` / `tmux not available in container` |
| Session created with correct backing | `Session rts-XXXXXXXX created (PTY spawned, tmux_backed=True/False)` |
| Scrollback seeded on recovery | `Recovered tmux session rts-` |
| Orphan reaper fired | `Orphan reaper: detaching session` |
| Client attach/detach | `client attached` / `client detached` |
| Canvas load from startup | `qa-m6: returning` or `discover-devcontainers:` |
| Startup script fallback | `Running startup script` |

### Grep command

```bash
grep -E "tmux available|tmux_backed|rts-test|client attached|Orphan" /tmp/rts-test.log
```

### Adding temporary debug logging

For a new feature under development, add `logger.debug(...)` lines at decision points:

```python
logger.debug("probe_tmux: container={} cached={}", container, self._tmux_cache.get(container))
logger.debug("session_new_handler: container={!r} tmux_cached={}", container, mgr._tmux_cache.get(container))
```

Remove or demote to `logger.trace()` before merging. Do NOT log terminal I/O bytes.

---

## Layer 4 — REST API smoke tests (WebFetch, no test-mode needed)

Non-puppeting endpoints work on any running server (including production port):

```
GET /api/sessions          → verify session list shape
GET /api/hubs              → verify hub discovery returned expected containers
GET /api/startup           → verify startup script returned expected cards
GET /api/config            → verify config fields
GET /api/widgets/system-info → verify widget data
```

Use these to verify that config changes, startup scripts, and canvas saves took effect without touching a browser.

**Pattern: verify startup script cards**

```
GET /api/startup
```
Returns card descriptors from the configured startup script. Assert that the right `exec`, `container`, and `type` fields are present — confirms the script is wired up correctly before any browser loads.

---

## Probing Rule: Always Use the Frontend Service Card

**Never probe `claude-usage` from the backend.** The backend must not call `claude-usage` directly via `docker exec`, subprocess, or any headless ConPTY session.

**Why:** `claude-usage` is a TUI that sends terminal capability queries (`\u001b[c`, `\u001b[?9001h`, etc.) on startup and waits for the terminal to respond. A headless PTY with no connected client never responds — the tool stalls for the full timeout and returns nothing.

**The only correct probe path:**

```
credential-manager widget (frontend)
  → opens WS to /ws/session/new?cmd=docker exec -it <util> claude-usage ...
  → xterm.js IS the terminal, responds to capability queries
  → claude-usage runs, outputs JSON
  → frontend parses JSON, POSTs to /api/credentials/{name}/probe-result
  → CredentialManager stores result
```

This applies everywhere — QA scripts, agent automation, test fixtures. If you need probe data in a test context, render the credential-manager widget or call `POST /api/credentials/{name}/probe-result` directly with fixture data. Never call `probe_usage` or `probe_usage_via_session` — those functions have been removed.

## Layer 5 — E2E QA workflow (agent team, real dependencies)

For features with significant frontend + backend integration, run the full E2E QA workflow described in [e2e-qa-workflow.md](e2e-qa-workflow.md). This spawns a team of 5 agents (Analyst, Designer, Implementer, Runner, Fixer) that design, implement, and iterate on E2E tests using Playwright + Electron. Unlike the layers above, this workflow defaults to **real external dependencies** (real Docker containers, real filesystem) rather than mocks — see the guide for when to use each.

---

## Coverage expectations per feature type

| Feature type | Required coverage |
|---|---|
| Session logic (create, destroy, scrollback) | Unit tests with MockPty |
| API route (new endpoint) | aiohttp_client test for happy path + error cases |
| Docker/subprocess integration | Mock `asyncio.create_subprocess_exec` at unit level; optionally smoke with real containers |
| Startup script (new built-in) | Unit test for card list shape; WebFetch `/api/startup` smoke on running server |
| Frontend + backend integration | E2E QA workflow ([e2e-qa-workflow.md](e2e-qa-workflow.md)) with Playwright + real dependencies |
| Frontend-only behaviour | Log inspection (server side) + WebFetch API endpoints; browser visual confirmation deferred to human QA |
| Config/canvas persistence | `test_config.py` pattern — use temp dirs via `tmp_path` fixture |

---

## Running tests

```bash
# All tests
python -m pytest tests/ -v

# Specific file
python -m pytest tests/test_sessions.py -v

# With real server (puppeting layer)
python -m claude_rts --port 3002 --test-mode --no-browser
# then run integration script or use WebFetch from Claude Code
```
