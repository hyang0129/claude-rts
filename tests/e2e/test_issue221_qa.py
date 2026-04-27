"""Playwright E2E tests for issue #221 human QA checklist.

Covers the four human-only verification items from the consolidated QA debt
backlog (issue #221, PR #220 Container Manager epic):

  S1 — Container Manager widget label is "Container Manager" (not "VM Manager")
  S2 — POST /api/containers/create auto-registers container in favorites
  S3 — Container Stats widget renders live CPU/MEM data
  S4 — /profiles is mounted inside a canvas-claude container (real_docker only)

Run all:
    python -m pytest tests/e2e/test_issue221_qa.py -v

Run S4 (requires Docker):
    python -m pytest tests/e2e/test_issue221_qa.py::TestProfilesMount -v -m real_docker
"""

import pytest

pw = pytest.importorskip("playwright")

from tests.e2e.conftest import cleanup_non_container_cards, refresh_container_card  # noqa: E402


@pytest.fixture(scope="module")
def dev_config_preset():
    return "container-manager"


# ── Helpers ───────────────────────────────────────────────────────────────────


def seed_containers(page, backend_port, containers):
    page.evaluate(
        """async ([port, containers]) => {
        await fetch(`http://localhost:${port}/api/test/containers`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(containers),
        });
    }""",
        [backend_port, containers],
    )


def seed_container_stats(page, backend_port, stats):
    page.evaluate(
        """async ([port, stats]) => {
        await fetch(`http://localhost:${port}/api/test/container-stats`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(stats),
        });
    }""",
        [backend_port, stats],
    )


def enable_test_create(page, backend_port, labels=None):
    """Enable test-mode container creation (bypasses real Docker)."""
    page.evaluate(
        """async ([port, labels]) => {
        await fetch(`http://localhost:${port}/api/test/container-create`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({create_config: {}, labels: labels || {}}),
        });
    }""",
        [backend_port, labels or {}],
    )


def create_container(page, backend_port, name, image="ubuntu:24.04"):
    return page.evaluate(
        """async ([port, name, image]) => {
        const r = await fetch(`http://localhost:${port}/api/containers/create`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name, image}),
        });
        return {status: r.status, body: await r.json()};
    }""",
        [backend_port, name, image],
    )


def get_favorites(page, backend_port):
    return page.evaluate(
        """async (port) => {
        const r = await fetch(`http://localhost:${port}/api/containers/favorites`);
        return r.json();
    }""",
        backend_port,
    )


def spawn_widget(page, widget_type):
    """Open context menu and spawn a widget by its data-widget type."""
    from tests.e2e.conftest import open_context_menu

    open_context_menu(page)
    page.locator(f"#context-menu .ctx-item[data-widget='{widget_type}']").click()
    # wait for a card with that widgetType to appear
    page.wait_for_function(
        f"""() => typeof cards !== 'undefined' &&
        cards.some(c => c.widgetType === '{widget_type}')""",
        timeout=8000,
    )


# ── S1: Container Manager label ───────────────────────────────────────────────


class TestContainerManagerLabel:
    """S1 — context menu shows 'Container Manager', not 'VM Manager'."""

    def test_context_menu_shows_container_manager_label(self, page):
        """The context menu widget item for container-manager is labelled correctly."""
        from tests.e2e.conftest import open_context_menu

        open_context_menu(page)
        item = page.locator("#context-menu .ctx-item[data-widget='container-manager']")
        assert item.count() == 1, "Container Manager item not found in context menu"
        text = item.inner_text().strip()
        assert "Container Manager" in text, f"Expected 'Container Manager' in label, got: {text!r}"
        assert "VM Manager" not in text, f"Old 'VM Manager' label still present: {text!r}"
        # Dismiss menu
        page.keyboard.press("Escape")

    def test_no_vm_manager_item_in_context_menu(self, page):
        """No legacy 'vm-manager' or 'VM Manager' item remains in the context menu."""
        from tests.e2e.conftest import open_context_menu

        open_context_menu(page)
        vm_items = page.locator("#context-menu .ctx-item[data-widget='vm-manager']")
        assert vm_items.count() == 0, "Legacy vm-manager widget item still exists in context menu"
        page.keyboard.press("Escape")


# ── S2: container_create → favorites ─────────────────────────────────────────


class TestContainerCreateFavorites:
    """S2 — POST /api/containers/create auto-registers container in favorites."""

    CONTAINER_NAME = "e2e-221-s2-qa"

    def test_created_container_appears_in_favorites_api(self, page, backend_port):
        """Container created via API is immediately visible in GET /api/containers/favorites."""
        enable_test_create(page, backend_port)
        result = create_container(page, backend_port, self.CONTAINER_NAME)
        assert result["status"] == 200, f"create failed: {result['body']}"

        favorites = get_favorites(page, backend_port)
        names = [f["name"] for f in favorites]
        assert self.CONTAINER_NAME in names, f"Created container '{self.CONTAINER_NAME}' not in favorites: {names}"

    def test_created_container_appears_in_widget_dom(self, page, backend_port):
        """Container Manager widget DOM shows the newly created container."""
        enable_test_create(page, backend_port)
        create_container(page, backend_port, self.CONTAINER_NAME)

        # Seed _test_containers so discover shows the new container as running
        seed_containers(
            page,
            backend_port,
            [
                {
                    "name": self.CONTAINER_NAME,
                    "status": "running",
                    "image": "ubuntu:24.04",
                    "labels": {"created_by": "canvas-claude"},
                }
            ],
        )

        cleanup_non_container_cards(page)
        refresh_container_card(page)

        # The container's remove button should appear (one per favorite)
        btn = page.locator(f"[data-container-remove='{self.CONTAINER_NAME}']")
        assert btn.count() >= 1, (
            f"Container '{self.CONTAINER_NAME}' remove button not found in Container Manager widget"
        )


# ── S3: Container Stats widget ────────────────────────────────────────────────


class TestContainerStatsWidget:
    """S3 — Container Stats widget renders live CPU/MEM data."""

    MOCK_STATS = [
        {
            "name": "qa-stats-container",
            "status": "running",
            "cpu_percent": "12.34%",
            "mem_usage": "128MiB",
            "mem_limit": "8GiB",
            "mem_percent": "1.56%",
            "net_io": "1MB / 2MB",
            "block_io": "0B / 0B",
            "pids": 5,
            "created_by": "canvas-claude",
        }
    ]

    def test_stats_widget_renders_container_data(self, page, backend_port):
        """Container Stats widget shows container name and CPU/MEM from injected stats."""
        seed_container_stats(page, backend_port, self.MOCK_STATS)
        cleanup_non_container_cards(page)

        spawn_widget(page, "container-stats")

        # Wait for widget body to contain the container name
        page.wait_for_function(
            """() => {
                const bodies = document.querySelectorAll('.widget-body');
                return Array.from(bodies).some(b => b.textContent.includes('qa-stats-container'));
            }""",
            timeout=10000,
        )

        # Verify CPU and memory data visible
        page.wait_for_function(
            """() => {
                const bodies = document.querySelectorAll('.widget-body');
                return Array.from(bodies).some(b =>
                    b.textContent.includes('12.34%') || b.textContent.includes('128MiB')
                );
            }""",
            timeout=5000,
        )

    def test_stats_widget_refresh_interval(self, page, backend_port):
        """Container Stats widget has a 5-second refresh interval."""
        seed_container_stats(page, backend_port, self.MOCK_STATS)

        refresh_ms = page.evaluate(
            """() => {
                const card = cards.find(c => c.widgetType === 'container-stats');
                return card ? card.refreshInterval : null;
            }"""
        )
        assert refresh_ms == 5000, f"Expected 5000ms refresh interval, got {refresh_ms}"
