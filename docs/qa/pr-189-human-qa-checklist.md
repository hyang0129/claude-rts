# Human QA Checklist — PR #189: Main Profile Credential Swap

> Feature: Replace `priority_profile` config key and `${priority_credential}` substitution with a persistent "main" credential slot at `/profiles/<main_profile_name>`. The Profile Manager "Set as in-use" button copies credentials into the main slot.

## Prerequisites

- Util container (`supreme-claudemander-util`) is running.
- At least one tracked profile exists with a `.credentials.json` file (e.g. `test-profile`).
- Launch: `python -m claude_rts --dev-config start-claude`

---

## 1. Profile Manager Widget — UI

| # | Step | Expected | Pass/Fail |
|---|------|----------|-----------|
| 1.1 | Open the app; find the Profile Manager widget on the canvas | Widget is visible | |
| 1.2 | Inspect the table headers in the Profile Manager | Headers include **"In Use"** (not "Priority") | |
| 1.3 | Each tracked profile row has a **"Set as in-use"** button | Button visible per row | |
| 1.4 | The **main profile slot** (default: `main`) does NOT appear as a row in the Profile Manager list | `main` is absent from the profile rows | |
| 1.5 | Profile rows still show burn rate / usage columns unchanged | Burn rate and usage data display normally | |

---

## 2. GET /api/profiles/main Endpoint

| # | Step | Expected | Pass/Fail |
|---|------|----------|-----------|
| 2.1 | `curl http://localhost:<port>/api/profiles/main` with the main slot unconfigured (credentials not copied yet) | Returns `{"main_profile_name": "main", "exists": false}` | |
| 2.2 | After "Set as in-use" is clicked (see §3), repeat the GET | Returns `{"main_profile_name": "main", "exists": true}` | |
| 2.3 | The response always contains both `main_profile_name` and `exists` fields | Both keys present; `exists` is a boolean | |

---

## 3. Set as In-Use — Credential Copy

| # | Step | Expected | Pass/Fail |
|---|------|----------|-----------|
| 3.1 | Click **"Set as in-use"** on a tracked profile (e.g. `test-profile`) | No error shown; button click completes | |
| 3.2 | Inside the util container: `docker exec supreme-claudemander-util ls /profiles/main/` | File `.credentials.json` is present | |
| 3.3 | Verify the content matches the source profile: `docker exec ... diff /profiles/test-profile/.credentials.json /profiles/main/.credentials.json` | Files are identical | |
| 3.4 | Repeat "Set as in-use" with a second profile | `/profiles/main/.credentials.json` is overwritten with the new profile's credentials | |
| 3.5 | Click "Set as in-use" on a non-existent / invalid profile name (inject via DevTools) | Server returns 4xx; UI shows an error or silently fails gracefully (no 500) | |

---

## 4. Start Claude Button — Warning Paths

| # | Step | Expected | Pass/Fail |
|---|------|----------|-----------|
| 4.1 | Launch `--dev-config start-claude` (main slot empty by default) | App loads; Profile Manager visible | |
| 4.2 | Click the **"Start Claude"** button on the terminal card **before** setting a profile as in-use | Terminal shows warning: **"Main profile has no credentials yet"** or **"No main profile configured"** | |
| 4.3 | No PTY command is sent to the terminal on an empty main slot | Terminal does not attempt to launch claude | |

---

## 5. Start Claude Button — Happy Path (after Set as In-Use)

| # | Step | Expected | Pass/Fail |
|---|------|----------|-----------|
| 5.1 | Click "Set as in-use" on `test-profile`; confirm credentials copied (§3.2) | Main slot exists | |
| 5.2 | Click **"Start Claude"** button on the terminal card | Terminal receives `env CLAUDE_CONFIG_DIR=/profiles/main claude` (or equivalent); Claude starts | |
| 5.3 | Swap the in-use profile to a different profile (§3.4) without restarting any terminals | Next "Start Claude" click launches Claude with the updated credentials | |

---

## 6. Canvas Claude Card — Profile Fallback

| # | Step | Expected | Pass/Fail |
|---|------|----------|-----------|
| 6.1 | Spawn a Canvas Claude card without specifying a profile explicitly | Card uses `/profiles/main` as `CLAUDE_CONFIG_DIR` | |
| 6.2 | `POST /api/claude/terminal/create` with no `profile` param | Response creates terminal; command uses `/profiles/main` path | |

---

## 7. Blueprint System

| # | Step | Expected | Pass/Fail |
|---|------|----------|-----------|
| 7.1 | Save and run a blueprint that uses `get_main_profile` action | Step returns the main profile path string (e.g. `/profiles/main`) | |
| 7.2 | Save and run a blueprint that uses `get_priority_profile` action | Blueprint fails with error: **"Unknown action: get_priority_profile"** | |
| 7.3 | Old saved blueprints using `get_priority_profile` display a clear error (not a silent hang or crash) | Error message surfaces in blueprint log | |

---

## 8. Config Migration

| # | Step | Expected | Pass/Fail |
|---|------|----------|-----------|
| 8.1 | Inspect the generated config in `~/.supreme-claudemander-dev/config.json` | Key is `main_profile_name`, NOT `priority_profile` | |
| 8.2 | Start the server with an old config that still has `priority_profile` key | Server starts without crash; `priority_profile` key is ignored | |

---

## 9. `${priority_credential}` Substitution Removed

| # | Step | Expected | Pass/Fail |
|---|------|----------|-----------|
| 9.1 | `POST /api/claude/terminal/create?cmd=echo+${priority_credential}` | Terminal prints the literal string `${priority_credential}` — no substitution occurs | |
| 9.2 | Old blueprints/commands containing `${priority_credential}` receive no substitution | The literal placeholder is passed verbatim to the shell | |

---

## 10. Discover Profiles — Main Slot Excluded

| # | Step | Expected | Pass/Fail |
|---|------|----------|-----------|
| 10.1 | `GET /api/profiles` response | The `main` profile name does NOT appear in the returned list | |
| 10.2 | Change `main_profile_name` in config to a custom value (e.g. `my-main`) and restart | `my-main` is excluded from the profile list; all other profiles appear | |

---

## 11. MCP Server Tool Description

| # | Step | Expected | Pass/Fail |
|---|------|----------|-----------|
| 11.1 | Read the `open_terminal` tool description from the MCP server JSON-RPC `tools/list` call | Description no longer references `${priority_credential}`; instructs caller to use `/profiles/main` directly | |

---

## Regression Checks

| # | Check | Expected | Pass/Fail |
|---|-------|----------|-----------|
| R.1 | `python -m pytest tests/ --ignore=tests/e2e -v` | All unit tests pass (≥ 438) | |
| R.2 | `python -m ruff check .` | No lint errors | |
| R.3 | `python -m ruff format --check .` | No formatting differences | |
| R.4 | All existing E2E smoke tests pass | `tests/e2e/test_smoke.py` green | |
| R.5 | `tests/e2e/test_start_claude.py` passes (requires Playwright + Electron) | All 3 tests green | |
