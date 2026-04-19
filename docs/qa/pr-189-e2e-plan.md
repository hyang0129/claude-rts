# PR #189 — E2E Automation Plan

**Feature:** Replace `priority_profile` config key and `${priority_credential}` substitution with a persistent "main" credential slot at `/profiles/<main_profile_name>`. The Profile Manager "Set as in-use" button copies credentials into the main slot.

**Source:** `docs/qa/pr-189-human-qa-checklist.md`

**Target preset:** `start-claude`

---

## 1. Summary Table — Human QA Item to Automation

Legend for **Automatable?**:
- **Yes** — fully automatable with existing infra (no Docker mutation required).
- **Yes (docker)** — automatable but requires the util container; marked `@skip_if_no_docker`.
- **Partial** — some sub-points automatable, others not.
- **No** — requires human observation, physical credentials, or real Claude binary.

| # | Human QA Item | Automatable? | Test (class::function) | Notes |
|---|---|---|---|---|
| 1.1 | Profile Manager widget visible | Yes | `TestProfileManagerWidget::test_profile_manager_widget_visible` | |
| 1.2 | Table header says "In Use" not "Priority" | Yes | `TestProfileManagerWidget::test_header_says_in_use_not_priority` | DOM scrape of `<th>` |
| 1.3 | Each row has "Set as in-use" button | Yes | `TestProfileManagerWidget::test_rows_have_set_as_in_use_button` | `[data-set-main]` selector |
| 1.4 | Main slot NOT in profile list rows | Yes | `TestProfileManagerWidget::test_main_slot_not_in_rows` | API + DOM assert |
| 1.5 | Burn rate / usage columns unchanged | Partial | `TestProfileManagerWidget::test_usage_columns_present` | Assert headers; live burn rates require Docker probe |
| 2.1 | GET `/api/profiles/main` returns `exists:false` when unconfigured | Yes (docker) | `TestMainProfileAPI::test_get_main_exists_false_when_unconfigured` | |
| 2.2 | GET `/api/profiles/main` returns `exists:true` after PUT | Yes (docker) | `TestMainProfileAPI::test_get_main_exists_true_after_put` | |
| 2.3 | Response has both `main_profile_name` and `exists` (bool) | Yes | `TestMainProfileAPI::test_main_response_contract` | |
| 3.1 | "Set as in-use" click completes without error | Yes (docker) | `TestSetAsInUse::test_set_as_in_use_click_ok` | |
| 3.2 | `/profiles/main/.credentials.json` exists after click | Yes (docker) | `TestSetAsInUse::test_credentials_file_created_in_main` | |
| 3.3 | File contents match source | Yes (docker) | `TestSetAsInUse::test_credentials_content_matches_source` | |
| 3.4 | Second click overwrites | Yes (docker) | `TestSetAsInUse::test_second_promotion_overwrites` | |
| 3.5 | Invalid profile name returns 4xx | Yes | `TestSetAsInUse::test_invalid_profile_returns_400` | API-only, no Docker |
| 4.1 | Launch start-claude; main slot empty | Yes | Covered by fixture startup | |
| 4.2 | Start Claude before creds shows warning | Yes | `TestStartClaudeButton::test_no_main_credentials_shows_warning` | **Existing** |
| 4.3 | No PTY command sent on empty slot | Yes | `TestStartClaudeButton::test_no_pty_write_when_main_missing` | **Gap** |
| 5.1 | Set as in-use confirms main slot exists | Yes (docker) | Covered by 2.2 + 3.2 | |
| 5.2 | Start Claude click sends env command | Yes (docker) | `TestStartClaudeButton::test_start_claude_sends_env_command` | **Gap** |
| 5.3 | Swapping profile updates subsequent launch | Yes (docker) | `TestStartClaudeButton::test_swap_main_profile_updates_next_launch` | **Gap** |
| 6.1 | Canvas Claude card without profile uses `/profiles/main` | Yes | `TestCanvasClaudeFallback::test_canvas_claude_card_defaults_to_main` | |
| 6.2 | `POST /api/canvas-claude/create` without profile uses `/profiles/main` | Yes | `TestCanvasClaudeFallback::test_canvas_claude_create_no_profile_param` | API-only |
| 7.1 | Blueprint `get_main_profile` returns main profile path | Yes | `TestMainProfileBlueprint::test_get_main_profile_step_returns_name` | `test_blueprint_e2e.py` |
| 7.2 | Blueprint `get_priority_profile` fails with clear error | Yes | `TestMainProfileBlueprint::test_legacy_get_priority_profile_rejected` | `test_blueprint_e2e.py` |
| 7.3 | Old saved blueprint error surfaces | Yes | `TestMainProfileBlueprint::test_legacy_blueprint_spawn_fails_visibly` | Already at unit level; E2E via save → 400 |
| 8.1 | Config file uses `main_profile_name` | Yes | `TestConfigMigration::test_config_uses_main_profile_name` | GET `/api/config` |
| 8.2 | Server with legacy `priority_profile` key ignored | No | — | Requires custom config dir; unit-covered |
| 9.1 | `${priority_credential}` not substituted | Yes | `TestLegacySubstitutionRemoved::test_priority_credential_not_substituted` | API-only |
| 10.1 | GET `/api/profiles` excludes `main` name | Yes (docker) | `TestProfileDiscovery::test_main_excluded_from_list` | |
| 10.2 | Custom `main_profile_name` excluded | No | — | Requires new preset; unit-testable |
| 11.1 | MCP `open_terminal` description no `${priority_credential}` | No (unit) | `tests/test_mcp_server.py` unit | stdio-only; not HTTP |
| R.1–R.3 | Lint/format/unit | No | CI separately | |

---

## 2. Detailed Test Specs

### 2.1 Shared Helpers (top of `test_start_claude.py`)

```python
import subprocess, requests

def _docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "ps"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False

skip_if_no_docker = pytest.mark.skipif(
    not _docker_available(), reason="Util container not available"
)

def _ensure_util_profile(name: str, creds: str = '{"token":"test"}') -> None:
    subprocess.run(
        ["docker", "exec", "supreme-claudemander-util", "sh", "-c",
         f"mkdir -p /profiles/{name} && printf '%s' '{creds}' > /profiles/{name}/.credentials.json"],
        check=True, timeout=15,
    )

def _read_main_creds() -> str:
    r = subprocess.run(
        ["docker", "exec", "supreme-claudemander-util", "cat", "/profiles/main/.credentials.json"],
        capture_output=True, text=True, timeout=10,
    )
    return r.stdout

def _clear_main_slot() -> None:
    subprocess.run(
        ["docker", "exec", "supreme-claudemander-util", "sh", "-c",
         "rm -rf /profiles/main"],
        check=True, timeout=10,
    )
```

---

### 2.2 `TestProfileManagerWidget` (browser, no Docker)

**Setup:** The `start-claude` preset pre-places a Profile Manager widget. Wait for its `render()` to complete by polling for `[data-set-main]` or `[data-profile-list]` to be in the DOM.

- **test_profile_manager_widget_visible** — `page.locator('.widget-card').first.wait_for(state='visible')`. Assert at least one card body contains "Set as in-use".
- **test_header_says_in_use_not_priority** — `page.evaluate(...)` to extract all `<th>` text content from the profiles widget; assert any equals `"In Use"`; assert none equals `"Priority"`.
- **test_rows_have_set_as_in_use_button** — `page.locator('[data-set-main]').count() > 0`. The start-claude preset's tracked profiles should each produce a button.
- **test_main_slot_not_in_rows** — `GET /api/profiles` via page.evaluate; assert no entry with `profile === "main"`. DOM: no `<tr>` first-cell text equals `"main"` exactly.
- **test_usage_columns_present** — Assert `<th>` cells include `"5h"`, `"7d"`, `"Burn/day"`, `"Resets"`, and `"In Use"`.

---

### 2.3 `TestMainProfileAPI` (API + optional docker)

- **test_main_response_contract** (no Docker) — `requests.get(f"http://localhost:{backend_port}/api/profiles/main")`. Assert status 200, response is JSON, has `main_profile_name == "main"` and `isinstance(exists, bool)`.
- **test_get_main_exists_false_when_unconfigured** (docker) — `_clear_main_slot()`, GET, assert `exists == False`.
- **test_get_main_exists_true_after_put** (docker) — `_ensure_util_profile("test-profile")`, PUT `{"source_profile":"test-profile"}`, GET, assert `exists == True`.

---

### 2.4 `TestSetAsInUse` (mixed)

- **test_invalid_profile_returns_400** (no Docker) — PUT `{"source_profile":"__nonexistent__"}` → 404 or 400. PUT `{"source_profile":"../evil"}` → 400 (invalid name).
- **test_set_as_in_use_click_ok** (docker) — `_ensure_util_profile("test-profile")`. `page.wait_for_selector('[data-set-main="test-profile"]')`. Use `page.expect_response("**/api/profiles/main")` context manager. Click button. Assert response status 200.
- **test_credentials_file_created_in_main** (docker) — After click, `_read_main_creds()` is non-empty.
- **test_credentials_content_matches_source** (docker) — Seed known JSON; after promotion, `_read_main_creds()` matches.
- **test_second_promotion_overwrites** (docker) — Promote profile A (unique cred), then profile B (different cred); final `_read_main_creds()` equals B's cred.

---

### 2.5 `TestStartClaudeButton` (existing + gaps)

**Existing (keep unchanged):** `test_claude_btn_visible_on_terminal_card`, `test_main_profile_api_returns_main`, `test_no_main_credentials_shows_warning`.

- **test_no_pty_write_when_main_missing** (no Docker) — Intercept WebSocket frames via `page.on("websocket", cb)`. Click `.claude-btn` (main slot empty). Assert no WS frame body contains `"CLAUDE_CONFIG_DIR"`. Also assert xterm buffer shows the warning string but not the env command.
- **test_start_claude_sends_env_command** (docker) — `_ensure_util_profile("test-profile")`, click "Set as in-use", wait for PUT 200. Click `.claude-btn`. Use `page.wait_for_function(...)` to poll xterm buffer until it contains `"CLAUDE_CONFIG_DIR=/profiles/main"` (echo from PTY) within 5s.
- **test_swap_main_profile_updates_next_launch** (docker) — Seed two profiles (A, B). Promote A → verify creds_A. Promote B → `_read_main_creds()` returns creds_B. Click Start Claude; assert xterm still uses `/profiles/main` path (path is stable; inner creds changed).

---

### 2.6 `TestCanvasClaudeFallback` (API, no Docker)

- **test_canvas_claude_create_no_profile_param** — POST `/api/canvas-claude/create?x=0&y=0&w=400&h=300` (no `profile` param). Assert 200. Cleanup: DELETE the returned terminal id.
- **test_canvas_claude_card_defaults_to_main** — `page.evaluate(...)` to find a canvas-claude card (if one exists) and assert `card.profile === "main"`, or spawn one via right-click context menu and assert.

---

### 2.7 `TestMainProfileBlueprint` in `test_blueprint_e2e.py`

- **test_get_main_profile_step_returns_name** — Save blueprint `{"steps":[{"action":"get_main_profile","out":"cred"}]}`. Spawn it. Poll `blueprint:completed` event (or inspect log cards). Assert output/log contains `"main"`.
- **test_legacy_get_priority_profile_rejected** — POST `/api/blueprints` with `{"steps":[{"action":"get_priority_profile"}]}`; assert 400 with error mentioning `"get_priority_profile"`. (If save validates, this alone suffices for 7.2 + 7.3.)
- **test_legacy_blueprint_spawn_fails_visibly** — Save valid blueprint, then mutate on disk OR directly POST an invalid blueprint and assert the save returns 400. E2E: observe `blueprint:failed` control-ws event with the error string.

---

### 2.8 `TestConfigMigration` (API, no Docker)

- **test_config_uses_main_profile_name** — GET `/api/config`. Assert `"main_profile_name"` in response JSON and `"priority_profile"` NOT in response JSON.

---

### 2.9 `TestLegacySubstitutionRemoved` (API, no Docker)

- **test_priority_credential_not_substituted** — POST `/api/claude/terminal/create?cmd=echo%20%24%7Bpriority_credential%7D`. Assert 200. Check the scrollback or `exec` field contains the literal string `${priority_credential}`. DELETE terminal on teardown.

---

### 2.10 `TestProfileDiscovery` (docker)

- **test_main_excluded_from_list** — `_ensure_util_profile("test-profile")` and `_ensure_util_profile("main")`. GET `/api/profiles`. Assert no entry has `profile == "main"`. Assert `test-profile` appears.

---

## 3. Test File Ownership

| File | Classes (new) | Docker required? |
|---|---|---|
| `tests/e2e/test_start_claude.py` | `TestProfileManagerWidget`, `TestMainProfileAPI`, `TestSetAsInUse`, `TestCanvasClaudeFallback`, `TestConfigMigration`, `TestLegacySubstitutionRemoved`, `TestProfileDiscovery` | Partial |
| `tests/e2e/test_start_claude.py` | `TestStartClaudeButton` (extend existing) | Partial |
| `tests/e2e/test_blueprint_e2e.py` | `TestMainProfileBlueprint` | No |

No new test files required.

---

## 4. Not Automatable / Deferred

| # | Reason |
|---|---|
| 8.2 | Requires custom config dir with legacy key; unit-covered by `test_config.py` |
| 10.2 | Requires new `start-claude-custom-main` dev-config preset — flagged for follow-up PR |
| 11.1 | MCP server is stdio-only; covered at unit level in `tests/test_mcp_server.py` |
| "Claude actually starts" (5.2 happy) | Requires real Anthropic API key; E2E only verifies the command is sent |

---

## 5. Gaps in Existing `test_start_claude.py`

The existing 3 tests are incomplete. This plan adds:
- `TestProfileManagerWidget` (5 tests) — covers 1.1–1.5
- `TestMainProfileAPI::test_main_response_contract` — strengthens 2.3
- `TestSetAsInUse` (5 tests) — covers 3.x
- `TestStartClaudeButton::test_no_pty_write_when_main_missing` — covers 4.3
- `TestStartClaudeButton::test_start_claude_sends_env_command` — covers 5.2
- `TestStartClaudeButton::test_swap_main_profile_updates_next_launch` — covers 5.3
- `TestCanvasClaudeFallback` (2 tests) — covers 6.x
- `TestConfigMigration` (1 test) — covers 8.1
- `TestLegacySubstitutionRemoved` (1 test) — covers 9.1
- `TestProfileDiscovery` (1 test) — covers 10.1

---

## 6. Implementation Order

1. **API-only, no Docker** (fastest, no browser): `TestMainProfileAPI::test_main_response_contract`, `TestSetAsInUse::test_invalid_profile_returns_400`, `TestLegacySubstitutionRemoved`, `TestConfigMigration`.
2. **Browser, no Docker**: `TestProfileManagerWidget`, `TestCanvasClaudeFallback`, `TestStartClaudeButton::test_no_pty_write_when_main_missing`.
3. **Blueprint**: `TestMainProfileBlueprint` in `test_blueprint_e2e.py`.
4. **Docker-gated**: `TestMainProfileAPI` (2.1/2.2), `TestSetAsInUse` (3.1–3.4), `TestStartClaudeButton` happy path (5.2/5.3), `TestProfileDiscovery`.
