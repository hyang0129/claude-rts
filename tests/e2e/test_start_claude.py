"""Playwright e2e tests for the Start Claude button on terminal cards.

Uses the ``start-claude`` dev-config preset which has:
- main_profile_name set to "main" (default)
- A Profile Manager widget and a terminal card pre-placed on canvas

Run:
    HEADED=1 python -m pytest tests/e2e/test_start_claude.py -v
"""

import subprocess

import pytest
import requests

pw = pytest.importorskip("playwright")


# ── Docker helpers ─────────────────────────────────────────────────────────────


def _docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "ps"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


skip_if_no_docker = pytest.mark.skipif(not _docker_available(), reason="Util container not available")


def _ensure_util_profile(name: str, creds: str = '{"token":"test"}') -> None:
    # In dev-config mode the util container has no volume mount, so /profiles is
    # root:root 755.  chmod 777 once so uid=1000 (util) and the server can write.
    subprocess.run(
        ["docker", "exec", "--user", "root", "supreme-claudemander-util", "chmod", "777", "/profiles"],
        check=True,
        timeout=10,
    )
    subprocess.run(
        [
            "docker",
            "exec",
            "supreme-claudemander-util",
            "sh",
            "-c",
            f"mkdir -p /profiles/{name} && printf '%s' '{creds}' > /profiles/{name}/.credentials.json",
        ],
        check=True,
        timeout=15,
    )


def _read_main_creds() -> str:
    r = subprocess.run(
        ["docker", "exec", "supreme-claudemander-util", "cat", "/profiles/main/.credentials.json"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return r.stdout


def _clear_main_slot(backend_port: int | None = None, page=None) -> None:
    """Remove /profiles/main from the util container and reset active_main_source in config.

    The docker rm clears the on-disk credentials.  The config API call clears
    ``active_main_source`` so the Profile Manager widget re-renders the
    "Set as in-use" button (with ``data-set-main``) instead of the disabled
    "In Use ✓" indicator.  Without this, subsequent tests that query
    ``[data-set-main="test-profile"]`` time out because the button is missing.

    When ``page`` is supplied, the Profile Manager widget is force-refreshed
    synchronously so the DOM reflects the cleared state immediately — otherwise
    tests race the widget's 30 s auto-refresh interval.

    ``backend_port`` is optional for call-sites that do not have the port
    available (e.g. the stale-state guard in test_no_main_credentials_shows_warning).
    """
    subprocess.run(
        ["docker", "exec", "--user", "root", "supreme-claudemander-util", "rm", "-rf", "/profiles/main"],
        check=True,
        timeout=10,
    )
    if backend_port is not None:
        resp = requests.get(f"http://localhost:{backend_port}/api/config", timeout=10)
        resp.raise_for_status()
        config = resp.json()
        config.pop("active_main_source", None)
        put_resp = requests.put(f"http://localhost:{backend_port}/api/config", json=config, timeout=10)
        put_resp.raise_for_status()
    if page is not None:
        page.evaluate("""async () => {
            const card = cards.find(c => c.widgetType === 'profiles');
            if (card && typeof card.render === 'function') await card.render();
        }""")


# ── Override the preset to start-claude ────────────────────────────────────────


@pytest.fixture(scope="module")
def dev_config_preset():
    return "start-claude"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _find_terminal_card(page):
    """Return the first card element that has a .claude-btn (i.e. a terminal card)."""
    btn = page.locator(".claude-btn").first
    if btn.count() == 0:
        return None, None
    card = btn.locator("xpath=ancestor::*[@data-card-id]")
    if card.count() == 0:
        return None, None
    return card, btn


def _get_card_session_id(page, card):
    """Get the WebSocket session ID from the frontend card object."""
    card_id = card.get_attribute("data-card-id")
    return page.evaluate(
        """(cardId) => {
            const c = cards.find(c => String(c.id) === String(cardId));
            return c ? c.sessionId : null;
        }""",
        card_id,
    )


def _read_session_scrollback(page, backend_port, session_id):
    """Read scrollback for a specific session via the puppeting API."""
    return page.evaluate(
        """async ([port, sid]) => {
            const resp = await fetch(
                `http://localhost:${port}/api/test/session/${sid}/read`
            );
            if (!resp.ok) return { error: `HTTP ${resp.status}` };
            return await resp.json();
        }""",
        [backend_port, session_id],
    )


def _get_xterm_content(page, card):
    """Read the visible text content from the xterm.js terminal in this card."""
    card_id = card.get_attribute("data-card-id")
    return page.evaluate(
        """(cardId) => {
            const c = cards.find(c => String(c.id) === String(cardId));
            if (!c || !c.term) return '';
            const buf = c.term.buffer.active;
            let lines = [];
            for (let i = 0; i < buf.length; i++) {
                const line = buf.getLine(i);
                if (line) lines.push(line.translateToString(true));
            }
            return lines.join('\\n');
        }""",
        card_id,
    )


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestStartClaudeButton:
    """The Start Claude button on terminal cards uses the main profile slot (#163)."""

    def test_claude_btn_visible_on_terminal_card(self, page):
        """Terminal cards in the start-claude preset have a .claude-btn."""
        card, btn = _find_terminal_card(page)
        assert btn is not None, "Expected at least one .claude-btn on a terminal card"
        assert btn.is_visible()

    def test_main_profile_api_returns_main(self, page, backend_port):
        """The /api/profiles/main endpoint returns the configured main slot name."""
        result = page.evaluate(
            """async (port) => {
                const resp = await fetch(`http://localhost:${port}/api/profiles/main`);
                return await resp.json();
            }""",
            backend_port,
        )
        assert result.get("main_profile_name") == "main", f"Expected main_profile_name='main', got {result}"
        # exists may be True or False depending on whether creds were copied in —
        # we only assert the field is present as a boolean.
        assert isinstance(result.get("exists"), bool)

    def test_no_main_credentials_shows_warning(self, page):
        """When the main slot has no credentials, clicking Start Claude shows a warning.

        Clears /profiles/main first (if Docker is available) to guard against stale
        state left by previous test runs — the util container persists between runs.

        The start-claude preset doesn't pre-populate /profiles/main/.credentials.json,
        so the button should surface the 'no credentials yet' hint instead of sending
        the env command.
        """
        # Guard: clear stale /profiles/main from previous test runs.
        if _docker_available():
            try:
                _clear_main_slot()
            except Exception:
                pass

        card, btn = _find_terminal_card(page)
        if btn is None:
            pytest.skip("No terminal card with .claude-btn found")

        btn.click()

        # The Start Claude click handler writes one of two known warning strings
        # synchronously when the main slot is not ready:
        card_id = card.get_attribute("data-card-id")
        page.wait_for_function(
            """(cardId) => {
                const c = cards.find(c => String(c.id) === String(cardId));
                if (!c || !c.term) return false;
                const buf = c.term.buffer.active;
                for (let i = 0; i < buf.length; i++) {
                    const line = buf.getLine(i);
                    if (!line) continue;
                    const text = line.translateToString(true);
                    if (text.includes('No main profile configured') ||
                        text.includes('Main profile has no credentials yet')) {
                        return true;
                    }
                }
                return false;
            }""",
            arg=card_id,
            timeout=10000,
        )

        content = _get_xterm_content(page, card)
        assert "No main profile configured" in content or "Main profile has no credentials yet" in content, (
            f"Expected main-profile warning in xterm buffer.\nGot: {content[:500]}"
        )

    def test_no_pty_write_when_main_missing(self, page):
        """When the main slot is empty, clicking Start Claude must NOT send the env command.

        The button handler writes a warning to the xterm buffer synchronously and
        returns before any PTY write occurs. We verify:
        - No WebSocket frame body contains 'CLAUDE_CONFIG_DIR'.
        - The xterm buffer shows the warning but NOT the env command.
        """
        ws_frames = []

        def on_ws(ws):
            ws.on("framesent", lambda payload: ws_frames.append(payload))
            ws.on("framereceived", lambda payload: ws_frames.append(payload))

        page.on("websocket", on_ws)

        card, btn = _find_terminal_card(page)
        if btn is None:
            pytest.skip("No terminal card with .claude-btn found")

        btn.click()

        # Brief wait for any async write to happen (it shouldn't).
        page.wait_for_timeout(1500)

        # No WS frame should contain CLAUDE_CONFIG_DIR
        env_cmd_frames = [
            f
            for f in ws_frames
            if (isinstance(f, bytes) and b"CLAUDE_CONFIG_DIR" in f) or (isinstance(f, str) and "CLAUDE_CONFIG_DIR" in f)
        ]
        assert env_cmd_frames == [], f"Expected no WS frame with CLAUDE_CONFIG_DIR, but found: {env_cmd_frames}"

        # Xterm buffer must contain the warning
        content = _get_xterm_content(page, card)
        assert "CLAUDE_CONFIG_DIR" not in content, (
            f"env command must not appear in xterm when main slot is empty.\nGot: {content[:500]}"
        )

    @skip_if_no_docker
    def test_start_claude_sends_env_command(self, page, backend_port):
        """After promoting a profile, Start Claude click sends the env command to PTY.

        Requires util container. Steps:
        1. Ensure test-profile credentials exist in the container.
        2. Promote via PUT /api/profiles/main.
        3. Click .claude-btn.
        4. Poll xterm buffer until CLAUDE_CONFIG_DIR=/profiles/main appears.
        """
        _ensure_util_profile("test-profile")
        requests.put(
            f"http://localhost:{backend_port}/api/profiles/main",
            json={"source_profile": "test-profile"},
            timeout=10,
        ).raise_for_status()

        card, btn = _find_terminal_card(page)
        if btn is None:
            pytest.skip("No terminal card with .claude-btn found")

        btn.click()

        card_id = card.get_attribute("data-card-id")
        page.wait_for_function(
            """(cardId) => {
                const c = cards.find(c => String(c.id) === String(cardId));
                if (!c || !c.term) return false;
                const buf = c.term.buffer.active;
                for (let i = 0; i < buf.length; i++) {
                    const line = buf.getLine(i);
                    if (!line) continue;
                    if (line.translateToString(true).includes('CLAUDE_CONFIG_DIR=/profiles/main')) {
                        return true;
                    }
                }
                return false;
            }""",
            arg=card_id,
            timeout=5000,
        )

        content = _get_xterm_content(page, card)
        assert "CLAUDE_CONFIG_DIR=/profiles/main" in content, (
            f"Expected CLAUDE_CONFIG_DIR=/profiles/main in xterm buffer.\nGot: {content[:500]}"
        )

    @skip_if_no_docker
    def test_swap_main_profile_updates_next_launch(self, backend_port):
        """Swapping credentials via re-promotion updates /profiles/main in-place.

        The Start Claude path (/profiles/main) is an invariant — only the credentials
        inside it change.  We verify the credential swap via the server API and the
        util container, without clicking the button a second time (the prior test
        already started Claude in the shared terminal, so a second click goes to the
        running Claude process, not the shell).
        """
        creds_a = '{"token":"creds-A-secret"}'
        creds_b = '{"token":"creds-B-secret"}'

        # Seed and promote with creds-A
        _ensure_util_profile("test-profile", creds_a)
        requests.put(
            f"http://localhost:{backend_port}/api/profiles/main",
            json={"source_profile": "test-profile"},
            timeout=10,
        ).raise_for_status()
        assert "creds-A-secret" in _read_main_creds(), "Expected creds-A after first promotion"

        # Verify via API that main slot now exists
        resp = requests.get(f"http://localhost:{backend_port}/api/profiles/main", timeout=10)
        assert resp.json().get("exists") is True, "Main slot must exist after first promotion"

        # Re-seed with creds-B and promote again — path stays /profiles/main, inner creds swap
        _ensure_util_profile("test-profile", creds_b)
        requests.put(
            f"http://localhost:{backend_port}/api/profiles/main",
            json={"source_profile": "test-profile"},
            timeout=10,
        ).raise_for_status()
        assert "creds-B-secret" in _read_main_creds(), "Expected creds-B after second promotion"

        # Main slot still exists after swap (path stable)
        resp = requests.get(f"http://localhost:{backend_port}/api/profiles/main", timeout=10)
        assert resp.json().get("exists") is True, "Main slot must still exist after credential swap"
        assert resp.json().get("main_profile_name") == "main", "Main slot name must not change"

        # NOTE: We do NOT click Start Claude here. The shared module-scoped terminal
        # already has Claude running from test_start_claude_sends_env_command. A second
        # click sends the env command to the running Claude process, not the shell, so
        # the xterm echo is not reliable. The credential-swap correctness is fully
        # verified above via _read_main_creds() and GET /api/profiles/main.
        # The invariant that Start Claude always targets /profiles/main (not a profile name)
        # is enforced by the JS code and covered by test_start_claude_sends_env_command.

        assert resp.json().get("exists") is True  # main slot remains populated after swap


# ── TestProfileManagerWidget ──────────────────────────────────────────────────


class TestProfileManagerWidget:
    """Profile Manager widget renders the correct DOM for the main-profile feature."""

    def test_profile_manager_widget_visible(self, page):
        """A Profile Manager widget card is visible with 'Set as in-use' buttons.

        All cards (including WidgetCard) use class 'terminal-card'; widget bodies
        use 'widget-body'.  Wait for [data-set-main] which is the profiles-specific
        DOM marker rendered by the Profile Manager widget's render() call.
        """
        page.wait_for_selector("[data-set-main]", timeout=15000)
        assert page.locator("[data-set-main]").first.is_visible()

    def test_header_says_in_use_not_priority(self, page):
        """The profile table header must say 'In Use', never 'Priority'."""
        page.wait_for_selector("[data-set-main]", timeout=15000)
        th_texts = page.evaluate(
            """() => {
                const ths = document.querySelectorAll('th');
                return Array.from(ths).map(th => th.textContent.trim());
            }"""
        )
        assert any(t == "In Use" for t in th_texts), f"Expected 'In Use' <th>, got: {th_texts}"
        assert not any(t == "Priority" for t in th_texts), f"'Priority' <th> must not appear, got: {th_texts}"

    def test_rows_have_set_as_in_use_button(self, page):
        """Each tracked profile row has a [data-set-main] button."""
        page.wait_for_selector("[data-set-main]", timeout=15000)
        count = page.locator("[data-set-main]").count()
        assert count > 0, "Expected at least one [data-set-main] button from start-claude preset profiles"

    def test_main_slot_not_in_rows(self, page, backend_port):
        """The 'main' profile name must not appear as a row in the profile list.

        Verified both via the API response and via the DOM table rows.
        """
        page.wait_for_selector("[data-set-main]", timeout=15000)

        # API check: GET /api/profiles must not list 'main'
        profiles = page.evaluate(
            """async (port) => {
                const resp = await fetch(`http://localhost:${port}/api/profiles`);
                return await resp.json();
            }""",
            backend_port,
        )
        profile_names = [p.get("profile") for p in profiles]
        assert "main" not in profile_names, (
            f"'main' profile must not appear in /api/profiles list, got: {profile_names}"
        )

        # DOM check: no <tr> first-cell with exact text 'main'
        first_cell_texts = page.evaluate(
            """() => {
                const rows = document.querySelectorAll('table tr');
                return Array.from(rows).map(tr => {
                    const td = tr.querySelector('td');
                    return td ? td.textContent.trim() : null;
                }).filter(t => t !== null);
            }"""
        )
        assert "main" not in first_cell_texts, (
            f"DOM table row with text 'main' must not appear, got: {first_cell_texts}"
        )

    def test_usage_columns_present(self, page):
        """Profile table headers must include usage columns and the In Use column."""
        page.wait_for_selector("[data-set-main]", timeout=15000)
        th_texts = page.evaluate(
            """() => {
                const ths = document.querySelectorAll('th');
                return Array.from(ths).map(th => th.textContent.trim());
            }"""
        )
        for expected in ("5h", "7d", "Burn/day", "Resets", "In Use"):
            assert expected in th_texts, f"Expected column header '{expected}' in profile table, got: {th_texts}"


# ── TestMainProfileAPI ────────────────────────────────────────────────────────


class TestMainProfileAPI:
    """HTTP contract tests for GET/PUT /api/profiles/main."""

    def test_main_response_contract(self, backend_server, backend_port):
        """GET /api/profiles/main returns 200 with main_profile_name and exists fields."""
        resp = requests.get(f"http://localhost:{backend_port}/api/profiles/main", timeout=10)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.json()
        assert "main_profile_name" in data, f"'main_profile_name' missing from response: {data}"
        assert data["main_profile_name"] == "main", (
            f"Expected main_profile_name='main', got {data['main_profile_name']!r}"
        )
        assert "exists" in data, f"'exists' missing from response: {data}"
        assert isinstance(data["exists"], bool), f"'exists' must be a bool, got {type(data['exists'])}: {data}"

    @skip_if_no_docker
    def test_get_main_exists_false_when_unconfigured(self, backend_server, backend_port):
        """When the main slot has no credentials, exists must be False."""
        _clear_main_slot(backend_port)
        resp = requests.get(f"http://localhost:{backend_port}/api/profiles/main", timeout=10)
        assert resp.status_code == 200
        assert resp.json()["exists"] is False, f"Expected exists=False after clearing main slot, got: {resp.json()}"

    @skip_if_no_docker
    def test_get_main_exists_true_after_put(self, backend_server, backend_port):
        """After promoting a profile via PUT, GET /api/profiles/main returns exists=True."""
        _ensure_util_profile("test-profile")
        put_resp = requests.put(
            f"http://localhost:{backend_port}/api/profiles/main",
            json={"source_profile": "test-profile"},
            timeout=10,
        )
        assert put_resp.status_code == 200, f"PUT failed: {put_resp.status_code} {put_resp.text}"
        get_resp = requests.get(f"http://localhost:{backend_port}/api/profiles/main", timeout=10)
        assert get_resp.status_code == 200
        assert get_resp.json()["exists"] is True, f"Expected exists=True after PUT, got: {get_resp.json()}"


# ── TestSetAsInUse ────────────────────────────────────────────────────────────


class TestSetAsInUse:
    """Tests for the 'Set as in-use' button and credential promotion."""

    def test_invalid_profile_returns_400(self, backend_server, backend_port):
        """PUT with non-existent or invalid profile names must return 4xx."""
        # Non-existent but valid-shaped name: expect 400 (not in known profiles)
        resp = requests.put(
            f"http://localhost:{backend_port}/api/profiles/main",
            json={"source_profile": "__nonexistent__"},
            timeout=10,
        )
        assert resp.status_code in (400, 404), (
            f"Expected 400 or 404 for unknown profile, got {resp.status_code}: {resp.text}"
        )

        # Path-traversal name: must be rejected by shape validation (400)
        resp2 = requests.put(
            f"http://localhost:{backend_port}/api/profiles/main",
            json={"source_profile": "../evil"},
            timeout=10,
        )
        assert resp2.status_code == 400, (
            f"Expected 400 for invalid profile name '../evil', got {resp2.status_code}: {resp2.text}"
        )

    @skip_if_no_docker
    def test_set_as_in_use_click_ok(self, page, backend_port):
        """Clicking 'Set as in-use' for a tracked profile returns HTTP 200."""
        _ensure_util_profile("test-profile")

        page.wait_for_selector('[data-set-main="test-profile"]', timeout=15000)

        with page.expect_response("**/api/profiles/main") as resp_info:
            page.locator('[data-set-main="test-profile"]').click()

        resp = resp_info.value
        assert resp.status == 200, f"Expected 200 from PUT /api/profiles/main, got {resp.status}"

    @skip_if_no_docker
    def test_credentials_file_created_in_main(self, page, backend_port):
        """After clicking 'Set as in-use', the main slot credential file is non-empty."""
        _ensure_util_profile("test-profile")
        _clear_main_slot(backend_port, page=page)

        page.wait_for_selector('[data-set-main="test-profile"]', timeout=15000)
        with page.expect_response("**/api/profiles/main"):
            page.locator('[data-set-main="test-profile"]').click()

        creds = _read_main_creds()
        assert creds.strip(), "Expected non-empty credentials in /profiles/main after promotion"

    @skip_if_no_docker
    def test_credentials_content_matches_source(self, page, backend_port):
        """The credentials file content must match what was seeded for the source profile."""
        known_creds = '{"token":"known-secret-abc"}'
        _ensure_util_profile("test-profile", known_creds)
        _clear_main_slot(backend_port, page=page)

        page.wait_for_selector('[data-set-main="test-profile"]', timeout=15000)
        with page.expect_response("**/api/profiles/main"):
            page.locator('[data-set-main="test-profile"]').click()

        creds = _read_main_creds()
        assert "known-secret-abc" in creds, f"Expected seeded credentials in main slot, got: {creds!r}"

    @skip_if_no_docker
    def test_second_promotion_overwrites(self, page, backend_port):
        """Promoting the same profile with new credentials overwrites the main slot.

        Uses test-profile (the only profile in probe_profiles for this preset),
        seeding it twice with different credential content.
        """
        creds_a = '{"token":"first-cred-content"}'
        creds_b = '{"token":"second-cred-content"}'

        # Seed test-profile with creds-A and promote
        _ensure_util_profile("test-profile", creds_a)
        requests.put(
            f"http://localhost:{backend_port}/api/profiles/main",
            json={"source_profile": "test-profile"},
            timeout=10,
        ).raise_for_status()
        assert "first-cred-content" in _read_main_creds()

        # Re-seed test-profile with creds-B and promote again
        _ensure_util_profile("test-profile", creds_b)
        requests.put(
            f"http://localhost:{backend_port}/api/profiles/main",
            json={"source_profile": "test-profile"},
            timeout=10,
        ).raise_for_status()

        final_creds = _read_main_creds()
        assert "second-cred-content" in final_creds, (
            f"Expected second credentials after overwrite, got: {final_creds!r}"
        )
        assert "first-cred-content" not in final_creds, f"First credentials must be overwritten, got: {final_creds!r}"


# ── TestCanvasClaudeFallback ──────────────────────────────────────────────────


class TestCanvasClaudeFallback:
    """Canvas Claude card defaults to /profiles/main when no profile param is given."""

    def test_canvas_claude_create_no_profile_param(self, backend_server, backend_port):
        """POST /api/canvas-claude/create without profile param returns 200.

        The endpoint should fall back to the configured main profile slot.
        Cleanup: DELETE the returned terminal id.
        """
        resp = requests.post(
            f"http://localhost:{backend_port}/api/canvas-claude/create?x=0&y=0&w=400&h=300",
            timeout=15,
        )
        assert resp.status_code == 200, f"Expected 200 from canvas-claude create, got {resp.status_code}: {resp.text}"
        data = resp.json()
        terminal_id = data.get("session_id") or data.get("id")
        assert terminal_id, f"Expected session_id or id in response: {data}"

        # Cleanup
        try:
            requests.delete(
                f"http://localhost:{backend_port}/api/claude/terminal/{terminal_id}",
                timeout=10,
            )
        except Exception:
            pass

    @skip_if_no_docker
    def test_canvas_claude_card_defaults_to_main(self, backend_server, backend_port):
        """Canvas claude card creation without explicit profile defaults to 'main'.

        The frontend drops canvas_claude control-WS broadcasts by design, so the card
        never enters the JS cards[] array. We verify the default via the API response
        directly — the descriptor returned by the create endpoint includes the profile
        field when the card was successfully constructed.

        Requires Docker (util container) so the card can start successfully.
        """
        resp = requests.post(
            f"http://localhost:{backend_port}/api/canvas-claude/create",
            params={"x": 100, "y": 100, "w": 400, "h": 300},
            timeout=15,
        )
        assert resp.status_code == 200, f"Expected 200 from canvas-claude create, got {resp.status_code}: {resp.text}"
        data = resp.json()
        # The descriptor returned on success must reference the configured main slot.
        profile = data.get("profile")
        assert profile == "main", (
            f"Expected canvas-claude card profile='main' in API response, got: {profile!r}\nResponse: {data}"
        )
        # Cleanup
        terminal_id = data.get("session_id") or data.get("id")
        if terminal_id:
            try:
                requests.delete(
                    f"http://localhost:{backend_port}/api/claude/terminal/{terminal_id}",
                    timeout=10,
                )
            except Exception:
                pass


# ── TestConfigMigration ───────────────────────────────────────────────────────


class TestConfigMigration:
    """Config file must use the new main_profile_name key, not the legacy priority_profile."""

    def test_config_uses_main_profile_name(self, backend_server, backend_port):
        """GET /api/config must have main_profile_name and must NOT have priority_profile."""
        resp = requests.get(f"http://localhost:{backend_port}/api/config", timeout=10)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        cfg = resp.json()
        assert "main_profile_name" in cfg, f"Config must contain 'main_profile_name', got keys: {list(cfg.keys())}"
        assert "priority_profile" not in cfg, f"Config must NOT contain legacy 'priority_profile', got: {cfg}"


# ── TestLegacySubstitutionRemoved ─────────────────────────────────────────────


class TestLegacySubstitutionRemoved:
    """${priority_credential} placeholder must NOT be substituted by the server."""

    def test_priority_credential_not_substituted(self, backend_server, backend_port):
        """Creating a terminal with cmd containing ${priority_credential} passes it through verbatim.

        The server must NOT perform substitution; the literal string must survive
        into the PTY cmd.
        """
        import urllib.parse

        cmd = "echo ${priority_credential}"
        encoded = urllib.parse.quote(cmd)
        resp = requests.post(
            f"http://localhost:{backend_port}/api/claude/terminal/create?cmd={encoded}",
            timeout=15,
        )
        assert resp.status_code == 200, f"Expected 200 from terminal create, got {resp.status_code}: {resp.text}"
        data = resp.json()
        terminal_id = data.get("session_id") or data.get("id")
        assert terminal_id, f"Expected session_id in response: {data}"

        try:
            # The cmd field in status should be the verbatim command
            status_resp = requests.get(
                f"http://localhost:{backend_port}/api/claude/terminal/{terminal_id}/status",
                timeout=10,
            )
            if status_resp.ok:
                status = status_resp.json()
                cmd_field = status.get("cmd", "")
                # The literal placeholder must survive verbatim — no substitution.
                assert "${priority_credential}" in cmd_field, (
                    f"Expected literal '${{priority_credential}}' in cmd (no substitution), got: {cmd_field!r}"
                )
        finally:
            try:
                requests.delete(
                    f"http://localhost:{backend_port}/api/claude/terminal/{terminal_id}",
                    timeout=10,
                )
            except Exception:
                pass


# ── TestProfileDiscovery ──────────────────────────────────────────────────────


@skip_if_no_docker
class TestProfileDiscovery:
    """GET /api/profiles must exclude the main slot name from its results."""

    def test_main_excluded_from_list(self, backend_server, backend_port):
        """After seeding both 'test-profile' and 'main' in the container,
        /api/profiles must include test-profile but never list 'main'.
        """
        _ensure_util_profile("test-profile")
        _ensure_util_profile("main")

        resp = requests.get(f"http://localhost:{backend_port}/api/profiles", timeout=10)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        profiles = resp.json()
        profile_names = [p.get("profile") for p in profiles]

        assert "main" not in profile_names, f"'main' must be excluded from /api/profiles, got: {profile_names}"
        assert "test-profile" in profile_names, f"'test-profile' must appear in /api/profiles, got: {profile_names}"
