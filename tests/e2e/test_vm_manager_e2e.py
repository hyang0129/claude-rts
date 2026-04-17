"""Playwright E2E tests for the VM Manager card.

These tests launch the real backend with --dev-config vm-manager and
verify VM Manager widget interactions via Playwright in a browser.

Run:
    pip install pytest-playwright playwright
    python -m playwright install chromium
    python -m pytest tests/e2e/test_vm_manager_e2e.py -v

Headed mode (shows the browser window):
    HEADED=1 python -m pytest tests/e2e/test_vm_manager_e2e.py -v
"""

import json

import pytest

# Skip module entirely if playwright is not installed
pw = pytest.importorskip("playwright")

# Shared helpers live in conftest (single source of truth — see issue #165).
from tests.e2e.conftest import cleanup_non_vm_cards, refresh_vm_card  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def dev_config_preset():
    """Use the vm-manager dev-config preset."""
    return "vm-manager"


# ── Helpers ───────────────────────────────────────────────────────────────────


def seed_containers(page, backend_port, containers):
    """PUT fake container data to the test puppeting API."""
    page.evaluate(
        """async ([port, containers]) => {
        await fetch(`http://localhost:${port}/api/test/vm-containers`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(containers),
        });
    }""",
        [backend_port, containers],
    )


def set_favorites(page, backend_port, favorites):
    """PUT favorites to the API."""
    page.evaluate(
        """async ([port, favorites]) => {
        await fetch(`http://localhost:${port}/api/vms/favorites`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(favorites),
        });
    }""",
        [backend_port, favorites],
    )


def get_favorites(page, backend_port):
    """GET favorites from the API."""
    return page.evaluate(
        """async (port) => {
        const r = await fetch(`http://localhost:${port}/api/vms/favorites`);
        return r.json();
    }""",
        backend_port,
    )


def get_test_vm_containers(page, backend_port):
    """GET test VM containers from the puppeting API."""
    return page.evaluate(
        """async (port) => {
        const r = await fetch(`http://localhost:${port}/api/test/vm-containers`);
        return r.json();
    }""",
        backend_port,
    )


def save_blueprint(page, backend_port, name, blueprint_def):
    """PUT a blueprint definition to the API."""
    page.evaluate(
        """async ([port, name, blueprint]) => {
        await fetch(`http://localhost:${port}/api/blueprints/${name}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(blueprint),
        });
    }""",
        [backend_port, name, blueprint_def],
    )


def ensure_vm_card_exists(page):
    """Ensure at least one VM Manager card exists; spawn one if needed."""
    vm_cards = page.locator("[data-card-id]").filter(has=page.locator("[data-vm-search]"))
    if vm_cards.count() > 0:
        return
    # Spawn from context menu
    viewport = page.locator("#viewport")
    viewport.click(button="right", position={"x": 500, "y": 100})
    ctx_menu = page.locator("#context-menu")
    ctx_menu.wait_for(state="visible", timeout=3000)
    widget_item = ctx_menu.locator('[data-widget="vm-manager"]')
    if widget_item.count() > 0:
        widget_item.click()
        # Wait for VM Manager card to appear in DOM (render complete => has search input).
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('[data-card-id] [data-vm-search]').length > 0",
                timeout=5000,
            )
        except Exception:
            pass  # Fall through to JS-direct fallback below.
    # Fallback: if context menu approach didn't spawn a card, use JS directly
    vm_cards_after = page.locator("[data-card-id]").filter(has=page.locator("[data-vm-search]"))
    if vm_cards_after.count() == 0:
        page.evaluate("() => CARD_TYPE_REGISTRY.spawn('widget', {widgetType: 'vm-manager', x: 100, y: 100})")
        page.wait_for_function(
            "() => document.querySelectorAll('[data-card-id] [data-vm-search]').length > 0",
            timeout=5000,
        )


# ── S1: VM Manager widget spawns from context menu ───────────────────────────


class TestVmManagerSpawn:
    """S1: VM Manager widget spawns from context menu."""

    def test_vm_manager_spawns_from_context_menu(self, page, backend_port):
        """Right-click canvas, click vm-manager widget, verify card appears."""
        # Clear all cards first
        page.evaluate(
            """() => {
            if (typeof cards !== 'undefined') {
                for (const card of cards) {
                    if (typeof card.destroy === 'function') card.destroy();
                }
                cards.length = 0;
            }
            const el = document.getElementById('canvas');
            if (el) el.innerHTML = '';
        }"""
        )
        # Wait for canvas DOM to reflect the cleared state (no card elements).
        page.wait_for_function(
            "() => document.querySelectorAll('[data-card-id]').length === 0"
            " && (typeof cards === 'undefined' || cards.length === 0)",
            timeout=3000,
        )

        # Right-click on viewport
        viewport = page.locator("#viewport")
        viewport.click(button="right", position={"x": 500, "y": 100})

        ctx_menu = page.locator("#context-menu")
        ctx_menu.wait_for(state="visible", timeout=3000)

        # Find and click vm-manager widget item
        widget_item = ctx_menu.locator('[data-widget="vm-manager"]')
        assert widget_item.count() > 0, "vm-manager should be in context menu"
        widget_item.click()

        # Wait for the VM Manager card to be added and rendered (search input present).
        page.wait_for_function(
            "() => document.querySelectorAll('[data-card-id] [data-vm-search]').length > 0",
            timeout=5000,
        )

        # Verify card appeared
        new_cards = page.locator("[data-card-id]")
        assert new_cards.count() > 0, "A card should appear after clicking vm-manager"

        # Verify card contains the VM search input
        search_input = page.locator("[data-vm-search]")
        assert search_input.count() > 0, "Card should contain [data-vm-search]"


# ── S2: Favorites render with correct status indicators ──────────────────────


class TestVmFavoritesStatus:
    """S2: Favorites render with correct status indicators."""

    def test_favorites_status_indicators(self, page, backend_port):
        """Seed containers, set favorites, verify status dots and button states."""
        # Seed 3 containers
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "alpha",
                    "state": "online",
                    "image": "node:18",
                    "status": "Up 2 hours",
                },
                {
                    "name": "beta",
                    "state": "offline",
                    "image": "postgres:15",
                    "status": "Exited",
                },
                {
                    "name": "gamma",
                    "state": "starting",
                    "image": "redis:7",
                    "status": "Created",
                },
            ],
        )

        # Set favorites
        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "alpha",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                },
                {
                    "name": "beta",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                },
                {
                    "name": "gamma",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                },
            ],
        )

        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Verify green dot for alpha (online)
        alpha_dot = page.evaluate(
            """() => {
            const spans = document.querySelectorAll('span[title="Online"]');
            for (const s of spans) {
                if (s.closest('[data-card-id]') && s.parentElement.textContent.includes('alpha')) {
                    return window.getComputedStyle(s).color;
                }
            }
            return null;
        }"""
        )
        assert alpha_dot is not None, "Alpha should have an Online indicator"

        # Verify beta has a Start button
        start_btn = page.locator('[data-vm-start="beta"]')
        assert start_btn.count() > 0, "Offline container beta should have Start button"

        # Verify alpha action buttons are NOT dimmed
        alpha_action = page.locator('[data-vm-action="alpha"]').first
        if alpha_action.count() > 0:
            style = alpha_action.get_attribute("style") or ""
            assert "opacity:0.4" not in style, "Online container actions should not be dimmed"

        # Verify beta action buttons ARE dimmed
        beta_action = page.locator('[data-vm-action="beta"]').first
        if beta_action.count() > 0:
            style = beta_action.get_attribute("style") or ""
            assert "opacity:0.4" in style or "pointer-events:none" in style, (
                "Offline container actions should be dimmed"
            )


# ── S3: Search discovers containers and adds to favorites ────────────────────


class TestVmSearch:
    """S3: Search discovers containers and adds to favorites."""

    def test_search_and_add_favorite(self, page, backend_port):
        """Type in search, find a container, click add, verify it joins favorites."""
        # Seed containers
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "containerA",
                    "state": "online",
                    "image": "img:a",
                    "status": "Up",
                },
                {
                    "name": "containerB",
                    "state": "online",
                    "image": "img:b",
                    "status": "Up",
                },
                {
                    "name": "containerC",
                    "state": "offline",
                    "image": "img:c",
                    "status": "Exited",
                },
                {
                    "name": "containerD",
                    "state": "online",
                    "image": "img:d",
                    "status": "Up",
                },
            ],
        )

        # Only containerA is a favorite
        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "containerA",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                }
            ],
        )

        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Click search input and type
        search_input = page.locator("[data-vm-search]").first
        search_input.click()
        search_input.fill("containerB")

        # Wait for search results to render the expected row.
        page.wait_for_function(
            "() => document.querySelector('[data-vm-add=\"containerB\"]') !== null",
            timeout=3000,
        )
        results = page.locator("[data-vm-search-results]").first
        assert results.is_visible(), "Search results should be visible"

        # Verify containerB appears in results
        add_btn = page.locator('[data-vm-add="containerB"]')
        assert add_btn.count() > 0, "containerB should appear in search results"

        # Click add — readiness check first, then await DOM confirmation.
        add_btn.wait_for(state="visible", timeout=3000)
        add_btn.click()
        # Wait for the new favorite's remove button to appear (render complete).
        page.wait_for_function(
            "() => document.querySelector('[data-vm-remove=\"containerB\"]') !== null",
            timeout=5000,
        )

        # Verify favorites now include containerB
        favs = get_favorites(page, backend_port)
        fav_names = [f["name"] for f in favs]
        assert "containerA" in fav_names, "containerA should still be a favorite"
        assert "containerB" in fav_names, "containerB should now be a favorite"


# ── S4: Remove container from favorites ──────────────────────────────────────


class TestVmRemoveFavorite:
    """S4: Remove container from favorites."""

    def test_remove_favorite(self, page, backend_port):
        """Click remove button, verify container is removed from favorites."""
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "web-app",
                    "state": "online",
                    "image": "node:18",
                    "status": "Up",
                },
                {
                    "name": "db-server",
                    "state": "online",
                    "image": "pg:15",
                    "status": "Up",
                },
            ],
        )

        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "web-app",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                },
                {
                    "name": "db-server",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                },
            ],
        )

        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Click remove for web-app
        remove_btn = page.locator('[data-vm-remove="web-app"]')
        assert remove_btn.count() > 0, "Remove button for web-app should exist"
        remove_btn.wait_for(state="visible", timeout=3000)
        remove_btn.click()
        # Wait for the row to leave the DOM (render reflects the removal).
        page.wait_for_function(
            "() => document.querySelector('[data-vm-remove=\"web-app\"]') === null",
            timeout=5000,
        )

        # Verify only db-server remains
        favs = get_favorites(page, backend_port)
        fav_names = [f["name"] for f in favs]
        assert "web-app" not in fav_names, "web-app should be removed"
        assert "db-server" in fav_names, "db-server should remain"

        # Verify web-app row is gone from the card
        remove_btn_after = page.locator('[data-vm-remove="web-app"]')
        assert remove_btn_after.count() == 0, "web-app row should be gone from card"


# ── S5: Start offline container via Start button ─────────────────────────────


class TestVmStartContainer:
    """S5: Start offline container via Start button."""

    def test_start_offline_container(self, page, backend_port):
        """Click Start on offline container, verify it becomes online."""
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "my-db",
                    "state": "offline",
                    "image": "postgres:15",
                    "status": "Exited",
                },
            ],
        )

        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "my-db",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                }
            ],
        )

        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Verify Start button exists
        start_btn = page.locator('[data-vm-start="my-db"]')
        assert start_btn.count() > 0, "Start button for my-db should exist"

        # Click Start
        start_btn.wait_for(state="visible", timeout=3000)
        start_btn.click()

        # Wait for container state transition + VM card re-render (Start button gone).
        page.wait_for_function(
            "() => document.querySelector('[data-vm-start=\"my-db\"]') === null",
            timeout=10000,
        )

        # Verify container is now online via API
        containers = get_test_vm_containers(page, backend_port)
        my_db = next((c for c in containers if c["name"] == "my-db"), None)
        assert my_db is not None, "my-db should exist in test containers"
        assert my_db["state"] == "online", "my-db should be online after start"

        # Verify Start button is gone (online containers don't show it)
        start_btn_after = page.locator('[data-vm-start="my-db"]')
        assert start_btn_after.count() == 0, "Start button should be gone for online container"


# ── S6: Terminal action button spawns a terminal card ────────────────────────


class TestVmTerminalAction:
    """S6: Blueprint action button spawns a card."""

    def test_action_spawns_terminal(self, page, backend_port):
        """Click blueprint action on online container, verify new card spawns."""
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "dev-web",
                    "state": "online",
                    "image": "node:18",
                    "status": "Up 2 hours",
                },
            ],
        )

        # Save a minimal blueprint that opens a local terminal (no container required)
        save_blueprint(
            page,
            backend_port,
            "test-shell",
            {"name": "test-shell", "steps": [{"action": "open_terminal"}]},
        )

        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "dev-web",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "blueprint": "test-shell"}],
                }
            ],
        )

        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Count existing cards
        initial_count = page.locator("[data-card-id]").count()

        # Click Terminal action
        action_btn = page.locator('[data-vm-action="dev-web"][data-action-idx="0"]')
        assert action_btn.count() > 0, "Terminal action button should exist"
        action_btn.click()

        # BlueprintCard registers on the server immediately; wait for it to appear
        page.wait_for_function(
            f"() => document.querySelectorAll('[data-card-id]').length > {initial_count}",
            timeout=5000,
        )

        new_count = page.locator("[data-card-id]").count()
        assert new_count > initial_count, "A new card should be spawned after clicking blueprint action"


# ── S7: Multiple blueprint actions on one favorite ───────────────────────────


class TestVmBlueprintAction:
    """S7: Multiple blueprint actions render and each spawns a card when clicked."""

    def test_blueprint_action_spawns_card(self, page, backend_port):
        """Click the second blueprint action, verify a new card spawns."""
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "claude-dev",
                    "state": "online",
                    "image": "ubuntu:22.04",
                    "status": "Up 1 hour",
                },
            ],
        )

        # Save a minimal blueprint (local terminal, no Docker dependency)
        save_blueprint(
            page,
            backend_port,
            "test-shell",
            {"name": "test-shell", "steps": [{"action": "open_terminal"}]},
        )

        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "claude-dev",
                    "type": "docker",
                    "actions": [
                        {"label": "Terminal", "blueprint": "test-shell"},
                        {"label": "Dev Shell", "blueprint": "test-shell"},
                    ],
                }
            ],
        )

        cleanup_non_vm_cards(page)
        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Count existing cards
        initial_count = page.locator("[data-card-id]").count()

        # Click second action (index 1)
        action_btn = page.locator('[data-vm-action="claude-dev"][data-action-idx="1"]')
        assert action_btn.count() > 0, "Second blueprint action button should exist"
        action_btn.click()

        # BlueprintCard registers on the server immediately; wait for it to appear
        page.wait_for_function(
            f"() => document.querySelectorAll('[data-card-id]').length > {initial_count}",
            timeout=10000,
        )

        new_count = page.locator("[data-card-id]").count()
        assert new_count > initial_count, "A new card should be spawned for blueprint action"


# ── S8: Action buttons disabled for offline containers ───────────────────────


class TestVmActionsDisabledOffline:
    """S8: Action buttons disabled for offline containers."""

    def test_offline_actions_dimmed(self, page, backend_port):
        """Verify action buttons are dimmed and non-interactive for offline containers."""
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "stopped-svc",
                    "state": "offline",
                    "image": "nginx:latest",
                    "status": "Exited",
                },
            ],
        )

        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "stopped-svc",
                    "type": "docker",
                    "actions": [
                        {"label": "Terminal", "type": "terminal"},
                        {
                            "label": "Logs",
                            "type": "terminal",
                            "shell_prefix": "tail -f /var/log/app.log",
                        },
                    ],
                }
            ],
        )

        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Verify action buttons are dimmed
        action_btn = page.locator('[data-vm-action="stopped-svc"]').first
        assert action_btn.count() > 0, "Action button should exist for stopped-svc"
        style = action_btn.get_attribute("style") or ""
        assert "opacity:0.4" in style or "pointer-events:none" in style, (
            "Offline container action buttons should be dimmed"
        )

        # Click should not spawn a new card
        initial_count = page.locator("[data-card-id]").count()
        # Force-click even though pointer-events:none (Playwright can bypass)
        # but the JS handler checks state !== 'online' and returns early
        action_btn.dispatch_event("click")
        # Give the event loop a chance to process any (incorrectly) queued spawn,
        # then confirm card count is unchanged.  We poll for a stable count
        # rather than sleeping.
        page.wait_for_function(
            f"() => document.querySelectorAll('[data-card-id]').length === {initial_count}",
            timeout=1500,
        )
        new_count = page.locator("[data-card-id]").count()
        assert new_count == initial_count, "No new card should spawn for offline container"


# ── S9: Configure actions dialog opens and saves ─────────────────────────────


class TestVmConfigureActions:
    """S9: Configure actions dialog opens and saves."""

    def test_configure_dialog_saves(self, page, backend_port):
        """Open configure dialog, edit JSON, save, verify updated actions."""
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "my-app",
                    "state": "online",
                    "image": "node:18",
                    "status": "Up 1 hour",
                },
            ],
        )

        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "my-app",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                }
            ],
        )

        cleanup_non_vm_cards(page)
        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Click configure button
        configure_btn = page.locator('[data-vm-configure="my-app"]')
        assert configure_btn.count() > 0, "Configure button should exist"
        configure_btn.wait_for(state="visible", timeout=3000)
        configure_btn.click()

        # Wait for the dialog overlay to appear.
        page.locator("div[style*='z-index: 100000']").first.wait_for(state="visible", timeout=3000)

        # Verify dialog appeared (fixed overlay with z-index: 100000)
        dialog = page.locator("div[style*='z-index: 100000']")
        assert dialog.count() > 0, "Configure dialog should appear"

        # Verify textarea contains current actions JSON
        textarea = page.locator("[data-actions-json]")
        assert textarea.count() > 0, "Actions JSON textarea should exist"
        value = textarea.input_value()
        assert "Terminal" in value, "Textarea should contain current actions JSON"

        # Clear and type new JSON
        new_actions = json.dumps(
            [
                {"label": "Terminal", "type": "terminal"},
                {
                    "label": "Logs",
                    "type": "terminal",
                    "shell_prefix": "tail -f /var/log/app.log",
                },
            ]
        )
        textarea.fill(new_actions)

        # Click Save
        save_btn = page.locator("[data-save]")
        save_btn.wait_for(state="visible", timeout=3000)
        save_btn.click()

        # Wait for the dialog overlay to be removed from the DOM.
        page.wait_for_function(
            "() => document.querySelectorAll(\"div[style*='z-index: 100000']\").length === 0",
            timeout=5000,
        )

        # Verify dialog disappeared
        dialog_after = page.locator("div[style*='z-index: 100000']")
        assert dialog_after.count() == 0, "Dialog should be closed after save"

        # Verify favorites were updated
        favs = get_favorites(page, backend_port)
        my_app_fav = next((f for f in favs if f["name"] == "my-app"), None)
        assert my_app_fav is not None, "my-app should still be a favorite"
        assert len(my_app_fav["actions"]) == 2, "my-app should now have 2 actions"
        assert my_app_fav["actions"][1]["label"] == "Logs"


# ── S10: Configure actions dialog cancel does not save ───────────────────────


class TestVmConfigureCancel:
    """S10: Configure actions dialog cancel does not save."""

    def test_configure_dialog_cancel(self, page, backend_port):
        """Open configure dialog, modify, cancel, verify actions unchanged."""
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "cancel-test",
                    "state": "online",
                    "image": "node:18",
                    "status": "Up",
                },
            ],
        )

        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "cancel-test",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                }
            ],
        )

        cleanup_non_vm_cards(page)
        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Open configure dialog
        configure_btn = page.locator('[data-vm-configure="cancel-test"]')
        configure_btn.wait_for(state="visible", timeout=3000)
        configure_btn.click()
        # Wait for the dialog overlay to appear.
        page.locator("div[style*='z-index: 100000']").first.wait_for(state="visible", timeout=3000)

        # Modify textarea
        textarea = page.locator("[data-actions-json]")
        textarea.fill('[{"label":"CHANGED","type":"terminal"}]')

        # Click Cancel
        cancel_btn = page.locator("[data-cancel]")
        cancel_btn.wait_for(state="visible", timeout=3000)
        cancel_btn.click()
        # Wait for the dialog overlay to be removed from the DOM.
        page.wait_for_function(
            "() => document.querySelectorAll(\"div[style*='z-index: 100000']\").length === 0",
            timeout=3000,
        )

        # Verify dialog closed
        dialog = page.locator("div[style*='z-index: 100000']")
        assert dialog.count() == 0, "Dialog should close on cancel"

        # Verify favorites unchanged
        favs = get_favorites(page, backend_port)
        fav = next((f for f in favs if f["name"] == "cancel-test"), None)
        assert fav is not None
        assert fav["actions"][0]["label"] == "Terminal", "Actions should be unchanged after cancel"


# ── S11: Configure actions dialog rejects invalid JSON ───────────────────────


class TestVmConfigureInvalidJson:
    """S11: Configure actions dialog rejects invalid JSON."""

    def test_configure_invalid_json_rejected(self, page, backend_port):
        """Type invalid JSON, click Save, verify alert and dialog stays open."""
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "json-test",
                    "state": "online",
                    "image": "node:18",
                    "status": "Up",
                },
            ],
        )

        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "json-test",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                }
            ],
        )

        cleanup_non_vm_cards(page)
        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Open configure dialog
        configure_btn = page.locator('[data-vm-configure="json-test"]')
        configure_btn.wait_for(state="visible", timeout=3000)
        configure_btn.click()
        # Wait for the dialog overlay to appear.
        page.locator("div[style*='z-index: 100000']").first.wait_for(state="visible", timeout=3000)

        # Type invalid JSON
        textarea = page.locator("[data-actions-json]")
        textarea.fill("not valid json")

        # Intercept dialog (alert)
        alert_message = []
        page.on("dialog", lambda dialog: (alert_message.append(dialog.message), dialog.accept()))

        # Click Save — the page.on("dialog", ...) handler fires synchronously
        # when the JS alert() is invoked during this click, so by the time
        # click() returns the alert_message list has been populated.
        save_btn = page.locator("[data-save]")
        save_btn.wait_for(state="visible", timeout=3000)
        save_btn.click()

        # Verify alert fired
        assert len(alert_message) > 0, "An alert should fire for invalid JSON"
        assert "Invalid JSON" in alert_message[0], "Alert should mention Invalid JSON"

        # Dialog should still be open
        dialog = page.locator("div[style*='z-index: 100000']")
        assert dialog.count() > 0, "Dialog should remain open after invalid JSON"

        # Clean up: close dialog
        cancel_btn = page.locator("[data-cancel]")
        if cancel_btn.count() > 0:
            cancel_btn.click()


# ── S12: Search filters out already-favorited containers ─────────────────────


class TestVmSearchFilters:
    """S12: Search filters out already-favorited containers."""

    def test_search_excludes_favorites(self, page, backend_port):
        """Search results should not include containers that are already favorites."""
        seed_containers(
            page,
            backend_port,
            [
                {"name": "aaa", "state": "online", "image": "img:1", "status": "Up"},
                {"name": "aab", "state": "online", "image": "img:2", "status": "Up"},
                {"name": "aac", "state": "offline", "image": "img:3", "status": "Exited"},
            ],
        )

        # aab is a favorite
        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "aab",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                }
            ],
        )

        cleanup_non_vm_cards(page)
        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Type search query
        search_input = page.locator("[data-vm-search]").first
        search_input.click()
        search_input.fill("aa")
        # Wait for the search results container to render with the expected rows
        # (aaa + aac; aab is filtered because it's a favorite).
        page.wait_for_function(
            "() => document.querySelector('[data-vm-add=\"aaa\"]') !== null"
            " && document.querySelector('[data-vm-add=\"aac\"]') !== null",
            timeout=3000,
        )

        # Verify search results
        add_aaa = page.locator('[data-vm-add="aaa"]')
        add_aab = page.locator('[data-vm-add="aab"]')
        add_aac = page.locator('[data-vm-add="aac"]')

        assert add_aaa.count() > 0, "aaa should be in search results"
        assert add_aab.count() == 0, "aab should NOT be in search results (already a favorite)"
        assert add_aac.count() > 0, "aac should be in search results"


# ── S13: Empty favorites shows placeholder message ───────────────────────────


class TestVmEmptyFavorites:
    """S13: Empty favorites shows placeholder message."""

    def test_empty_favorites_placeholder(self, page, backend_port):
        """When favorites is empty, show the placeholder text."""
        seed_containers(page, backend_port, [])
        set_favorites(page, backend_port, [])

        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Verify placeholder text
        body_text = page.evaluate(
            """() => {
            const cards = document.querySelectorAll('[data-card-id]');
            for (const card of cards) {
                const search = card.querySelector('[data-vm-search]');
                if (search) return card.textContent;
            }
            return '';
        }"""
        )
        assert "No favorites yet" in body_text, "Empty favorites should show placeholder message"


# ── S14: VM Manager card persists across canvas save/reload ──────────────────


class TestVmPersistence:
    """S14: VM Manager card persists across canvas save/reload."""

    def test_vm_card_persists_reload(self, page, backend_port):
        """Save canvas, reload, verify VM Manager card is still present."""
        # Seed some data so the card has content
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "persist-test",
                    "state": "online",
                    "image": "node:18",
                    "status": "Up",
                },
            ],
        )
        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "persist-test",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                }
            ],
        )

        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Verify VM Manager card exists
        vm_search = page.locator("[data-vm-search]")
        assert vm_search.count() > 0, "VM Manager card should exist before save"

        # Save layout and await completion of the /api/canvases/* PUT.
        page.evaluate(
            """async () => {
            if (typeof saveLayout === 'function') await saveLayout();
        }"""
        )

        # Reload
        page.goto(f"http://localhost:{backend_port}")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#canvas", timeout=10000)
        # Wait for boot-complete signal and for the VM Manager card to be restored
        # (search input present), rather than a fixed 3-second sleep.
        page.wait_for_function(
            "() => window.__claudeRtsBootComplete === true",
            timeout=15000,
        )
        page.wait_for_function(
            "() => document.querySelectorAll('[data-vm-search]').length > 0",
            timeout=10000,
        )

        # Verify VM Manager card is restored
        vm_search_after = page.locator("[data-vm-search]")
        assert vm_search_after.count() > 0, "VM Manager card should persist after reload"


# ── S15: Stop button stops running container ───────────────────────────────


class TestVmStopContainer:
    """S15: Stop running container via Stop button."""

    def test_stop_online_container(self, page, backend_port):
        """Click Stop on online container, verify it becomes offline."""
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "running-svc",
                    "state": "online",
                    "image": "node:18",
                    "status": "Up 2 hours",
                },
            ],
        )

        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": "running-svc",
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                }
            ],
        )

        cleanup_non_vm_cards(page)
        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Verify Stop button exists for running container
        stop_btn = page.locator('[data-vm-stop="running-svc"]')
        assert stop_btn.count() > 0, "Stop button for running-svc should exist"

        # Verify no Start button for running container
        start_btn = page.locator('[data-vm-start="running-svc"]')
        assert start_btn.count() == 0, "Start button should not exist for online container"

        # Click Stop
        stop_btn.wait_for(state="visible", timeout=3000)
        stop_btn.click()

        # Wait for container state transition + VM card re-render: Stop button
        # should vanish and Start button should appear.
        page.wait_for_function(
            "() => document.querySelector('[data-vm-stop=\"running-svc\"]') === null"
            " && document.querySelector('[data-vm-start=\"running-svc\"]') !== null",
            timeout=10000,
        )

        # Verify container is now offline via API
        containers = get_test_vm_containers(page, backend_port)
        svc = next((c for c in containers if c["name"] == "running-svc"), None)
        assert svc is not None, "running-svc should exist in test containers"
        assert svc["state"] == "offline", "running-svc should be offline after stop"

        # Verify Stop button is gone and Start button appeared
        stop_btn_after = page.locator('[data-vm-stop="running-svc"]')
        assert stop_btn_after.count() == 0, "Stop button should be gone for offline container"

        start_btn_after = page.locator('[data-vm-start="running-svc"]')
        assert start_btn_after.count() > 0, "Start button should appear for offline container"


# ── S16: Search results show richer metadata and sort online-first ─────────


class TestVmSearchMetadata:
    """S16: Search results show image, status text, and sort online first."""

    def test_search_results_metadata_and_sort(self, page, backend_port):
        """Search results show image and status, with online containers first."""
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": "svc-alpha",
                    "state": "offline",
                    "image": "postgres:15",
                    "status": "Exited 3 hours ago",
                },
                {
                    "name": "svc-beta",
                    "state": "online",
                    "image": "node:18",
                    "status": "Up 2 hours",
                },
                {
                    "name": "svc-gamma",
                    "state": "online",
                    "image": "redis:7",
                    "status": "Up 5 hours",
                },
            ],
        )

        # No favorites so all show in search
        set_favorites(page, backend_port, [])

        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Type search query
        search_input = page.locator("[data-vm-search]").first
        search_input.click()
        search_input.fill("svc-")
        # Wait for the search-results container to render all 3 expected rows.
        page.wait_for_function(
            "() => document.querySelectorAll('[data-vm-search-results] [data-vm-add]').length === 3",
            timeout=3000,
        )

        # Get all search result rows
        results = page.locator("[data-vm-search-results] [data-vm-add]")
        count = results.count()
        assert count == 3, f"Should find 3 results, got {count}"

        # Verify online containers appear first (svc-beta and svc-gamma before svc-alpha)
        first_name = results.nth(0).get_attribute("data-vm-add")
        second_name = results.nth(1).get_attribute("data-vm-add")
        third_name = results.nth(2).get_attribute("data-vm-add")
        assert first_name in ("svc-beta", "svc-gamma"), f"First result should be online, got {first_name}"
        assert second_name in ("svc-beta", "svc-gamma"), f"Second result should be online, got {second_name}"
        assert third_name == "svc-alpha", f"Third result should be offline svc-alpha, got {third_name}"

        # Verify search results contain image text
        result_html = page.locator("[data-vm-search-results]").first.inner_html()
        assert "node:18" in result_html, "Search results should show image name"
        assert "postgres:15" in result_html, "Search results should show image name"

        # Verify search results contain status text
        assert "Up 2 hours" in result_html or "Up 5 hours" in result_html, "Search results should show status text"
