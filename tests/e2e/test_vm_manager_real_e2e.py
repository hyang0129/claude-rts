"""Playwright E2E tests for the VM Manager card using REAL Docker containers.

These tests launch the real backend with --dev-config vm-manager and exercise
the actual docker.exe code paths (no test puppeting API, no mock container
data). Real containers are created/destroyed per test or per module.

Requires Docker Desktop to be running. Skipped entirely if docker.exe is
not available.

Run:
    pip install pytest-playwright playwright
    python -m playwright install chromium
    python -m pytest tests/e2e/test_vm_manager_real_e2e.py -v

Headed mode (shows the browser window):
    HEADED=1 python -m pytest tests/e2e/test_vm_manager_real_e2e.py -v
"""

import subprocess
import uuid

import pytest

# Skip module entirely if playwright is not installed
pw = pytest.importorskip("playwright")


# ── Markers & skip logic ─────────────────────────────────────────────────────

pytestmark = pytest.mark.real_docker


def _docker_available() -> bool:
    """Return True if docker.exe ps succeeds."""
    try:
        r = subprocess.run(
            ["docker.exe", "ps"],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


if not _docker_available():
    pytest.skip("Docker is not available (docker.exe ps failed)", allow_module_level=True)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def dev_config_preset():
    """Use the vm-manager dev-config preset."""
    return "vm-manager"


@pytest.fixture(scope="module")
def running_container():
    """Create a real running Alpine container for the test module."""
    name = f"e2e-vm-running-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker.exe", "run", "-d", "--name", name, "alpine:latest", "sleep", "3600"],
        check=True,
        capture_output=True,
    )
    yield name
    subprocess.run(
        ["docker.exe", "rm", "-f", name],
        capture_output=True,
    )


@pytest.fixture(scope="module")
def stopped_container():
    """Create a real exited Alpine container for the test module.

    Runs 'echo done' which exits immediately, producing state=exited
    which normalizes to 'offline'.
    """
    name = f"e2e-vm-stopped-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker.exe", "run", "--name", name, "alpine:latest", "echo", "done"],
        check=True,
        capture_output=True,
    )
    yield name
    subprocess.run(
        ["docker.exe", "rm", "-f", name],
        capture_output=True,
    )


@pytest.fixture()
def startable_container():
    """Create a fresh exited container per test (for start tests).

    Runs ``sleep 3600`` then immediately stops it so the container is in
    *exited* state (normalized to 'offline') but retains the long-running
    command.  When ``docker start`` is called, the container stays running.

    Using ``docker run … echo done`` would cause the container to exit
    immediately after start (same short-lived command re-runs).
    Using ``docker create`` produces state *created* which normalizes to
    'starting', not 'offline', so the Start button never appears.

    After the test, the container is force-removed regardless of state.
    """
    name = f"e2e-vm-startable-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker.exe", "run", "-d", "--name", name, "alpine:latest", "sleep", "3600"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["docker.exe", "stop", name],
        check=True,
        capture_output=True,
        timeout=15,
    )
    yield name
    subprocess.run(
        ["docker.exe", "rm", "-f", name],
        capture_output=True,
    )


@pytest.fixture()
def bash_container():
    """Create a running container with bash available (for terminal exec tests).

    Uses the bash:latest image which includes bash.
    """
    name = f"e2e-vm-bash-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker.exe", "run", "-d", "--name", name, "bash:latest", "sleep", "3600"],
        check=True,
        capture_output=True,
    )
    yield name
    subprocess.run(
        ["docker.exe", "rm", "-f", name],
        capture_output=True,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def set_favorites(page, port, favorites):
    """PUT favorites to the API."""
    page.evaluate(
        """async ([port, favorites]) => {
        await fetch(`http://localhost:${port}/api/vms/favorites`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(favorites),
        });
    }""",
        [port, favorites],
    )


def get_favorites(page, port):
    """GET favorites from the API."""
    return page.evaluate(
        """async (port) => {
        const r = await fetch(`http://localhost:${port}/api/vms/favorites`);
        return r.json();
    }""",
        port,
    )


def refresh_vm_card(page):
    """Force re-render of all VM Manager widget cards on the canvas."""
    page.evaluate(
        """() => {
        if (typeof cards !== 'undefined') {
            for (const card of cards) {
                if (card.widgetType === 'vm-manager' && typeof card.render === 'function') {
                    card.render();
                }
            }
        }
    }"""
    )
    page.wait_for_timeout(3000)


def cleanup_non_vm_cards(page):
    """Remove all cards except VM Manager widgets to prevent overlap issues."""
    page.evaluate(
        """() => {
        if (typeof cards === 'undefined') return;
        const toRemove = [];
        for (let i = cards.length - 1; i >= 0; i--) {
            if (cards[i].widgetType !== 'vm-manager') {
                if (typeof cards[i].destroy === 'function') cards[i].destroy();
                toRemove.push(i);
            }
        }
        for (const idx of toRemove) cards.splice(idx, 1);
    }"""
    )
    page.wait_for_function(
        """() => {
            const all = document.querySelectorAll('[data-card-id]');
            return Array.from(all).every(
                c => c.querySelector('[data-vm-search]') !== null
            );
        }""",
        timeout=3000,
    )


def ensure_vm_card_exists(page):
    """Ensure at least one VM Manager card exists; spawn one if needed."""
    vm_cards = page.locator("[data-card-id]").filter(has=page.locator("[data-vm-search]"))
    if vm_cards.count() > 0:
        return
    # Spawn via JS directly
    page.evaluate("() => CARD_TYPE_REGISTRY.spawn('widget', {widgetType: 'vm-manager', x: 100, y: 100})")
    page.wait_for_timeout(2000)


def docker_inspect_state(name: str) -> str:
    """Return the Docker container state (e.g. 'running', 'exited')."""
    r = subprocess.run(
        ["docker.exe", "inspect", "--format", "{{.State.Status}}", name],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


# ── S1: Discovery returns real containers with correct state mapping ─────────


class TestRealDiscovery:
    """S1: Discovery shows real running/stopped containers with correct indicators."""

    def test_real_containers_status_indicators(self, page, backend_port, running_container, stopped_container):
        """Real running container shows Online, exited container shows Offline."""
        # Set both containers as favorites
        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": running_container,
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                },
                {
                    "name": stopped_container,
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                },
            ],
        )

        cleanup_non_vm_cards(page)
        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Verify running container has Online indicator
        running_online = page.evaluate(
            """(name) => {
            const spans = document.querySelectorAll('span[title="Online"]');
            for (const s of spans) {
                if (s.closest('[data-card-id]') && s.parentElement.textContent.includes(name)) {
                    return true;
                }
            }
            return false;
        }""",
            running_container,
        )
        assert running_online, "Running container should have an Online indicator"

        # Verify stopped container has Offline indicator
        stopped_offline = page.evaluate(
            """(name) => {
            const spans = document.querySelectorAll('span[title="Offline"]');
            for (const s of spans) {
                if (s.closest('[data-card-id]') && s.parentElement.textContent.includes(name)) {
                    return true;
                }
            }
            return false;
        }""",
            stopped_container,
        )
        assert stopped_offline, "Stopped container should have an Offline indicator"

        # Running container should NOT have a Start button
        start_btn_running = page.locator(f'[data-vm-start="{running_container}"]')
        assert start_btn_running.count() == 0, "Running container should not have a Start button"

        # Stopped container SHOULD have a Start button
        start_btn_stopped = page.locator(f'[data-vm-start="{stopped_container}"]')
        assert start_btn_stopped.count() > 0, "Stopped container should have a Start button"


# ── S2: Start a real stopped container ───────────────────────────────────────


class TestRealStartContainer:
    """S2: Start a real stopped container and verify state transition."""

    def test_start_real_container(self, page, backend_port, startable_container):
        """Click Start on a real exited container, verify it becomes running."""
        # Add the startable container to favorites
        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": startable_container,
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                },
            ],
        )

        cleanup_non_vm_cards(page)
        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Verify Start button exists
        start_btn = page.locator(f'[data-vm-start="{startable_container}"]')
        assert start_btn.count() > 0, "Start button should exist for exited container"

        # Click Start
        start_btn.click()

        # Wait for the container to start and the card to re-render
        # The frontend does setTimeout(() => card.render(), 2000) after success
        page.wait_for_timeout(4000)

        # Verify container is actually running via docker inspect
        state = docker_inspect_state(startable_container)
        assert state == "running", f"Container should be running after start, got: {state}"

        # Verify Start button is gone after re-render
        start_btn_after = page.locator(f'[data-vm-start="{startable_container}"]')
        assert start_btn_after.count() == 0, "Start button should disappear after container starts"

        # Verify Online indicator appeared
        online_indicator = page.evaluate(
            """(name) => {
            const spans = document.querySelectorAll('span[title="Online"]');
            for (const s of spans) {
                if (s.closest('[data-card-id]') && s.parentElement.textContent.includes(name)) {
                    return true;
                }
            }
            return false;
        }""",
            startable_container,
        )
        assert online_indicator, "Container should show Online indicator after start"


# ── S3: Start a non-existent container surfaces error ────────────────────────


class TestRealStartNonExistent:
    """S3: A favorite that Docker has never seen shows 'not found' — no Start button."""

    def test_start_nonexistent_container_error(self, page, backend_port):
        """A favorite unknown to Docker renders as 'not found', not as startable/offline.

        Containers that have never been created in Docker cannot be started via
        ``docker start``, so the UI renders them with a "not found" label instead
        of a Start button.  The remove button must still be present so the user
        can clean up the stale favorite.
        """
        bogus_name = f"e2e-nonexistent-{uuid.uuid4().hex[:8]}"

        # Add bogus name to favorites
        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": bogus_name,
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                },
            ],
        )

        cleanup_non_vm_cards(page)
        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # The bogus container is not in Docker's list — UI renders it as "missing"
        # with a "not found" label, NOT a Start button.
        start_btn = page.locator(f'[data-vm-start="{bogus_name}"]')
        assert start_btn.count() == 0, "Start button should NOT exist for a container Docker has never seen"

        # Remove button must still be present so the user can clean up the stale entry
        remove_btn = page.locator(f'[data-vm-remove="{bogus_name}"]')
        assert remove_btn.count() > 0, "Remove button should exist for unknown favorite"

        # The row should display "not found" to distinguish it from a stopped container
        parent_text = page.evaluate(
            """(name) => {
                const btn = document.querySelector(`[data-vm-remove="${name}"]`);
                return btn ? btn.closest('div').innerText : '';
            }""",
            bogus_name,
        )
        assert "not found" in parent_text.lower(), (
            f"Row should contain 'not found' for undiscovered container, got: {parent_text}"
        )


# ── S4: Search shows real containers and adding one persists ─────────────────


class TestRealSearch:
    """S4: Search discovers real containers and adds to favorites."""

    def test_search_and_add_real_container(self, page, backend_port, running_container):
        """Type container name in search, click add, verify it joins favorites."""
        # Start with empty favorites
        set_favorites(page, backend_port, [])

        cleanup_non_vm_cards(page)
        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Type a substring of the running container's name in search
        search_input = page.locator("[data-vm-search]").first
        search_input.click()
        # Use the e2e-vm-running prefix to find our container
        search_input.fill("e2e-vm-running")

        # Wait for search results
        page.wait_for_timeout(1000)
        results = page.locator("[data-vm-search-results]").first
        assert results.is_visible(), "Search results should be visible"

        # Verify our running container appears in results with an Add button
        add_btn = page.locator(f'[data-vm-add="{running_container}"]')
        assert add_btn.count() > 0, f"Container {running_container} should appear in search results"

        # Click add
        add_btn.click()
        page.wait_for_timeout(2000)

        # Verify favorites now include the container
        favs = get_favorites(page, backend_port)
        fav_names = [f["name"] for f in favs]
        assert running_container in fav_names, f"{running_container} should now be a favorite"

        # Verify the container shows in the favorites list with Online indicator
        online_indicator = page.evaluate(
            """(name) => {
            const spans = document.querySelectorAll('span[title="Online"]');
            for (const s of spans) {
                if (s.closest('[data-card-id]') && s.parentElement.textContent.includes(name)) {
                    return true;
                }
            }
            return false;
        }""",
            running_container,
        )
        assert online_indicator, "Added container should show Online indicator"


# ── S5: Terminal action on a real running container spawns a card ─────────────


class TestRealTerminalAction:
    """S5: Terminal action on a real running container spawns a terminal card."""

    def test_terminal_action_spawns_card(self, page, backend_port, bash_container):
        """Click Terminal action on a real running container, verify card spawns."""
        # Add the bash container to favorites
        set_favorites(
            page,
            backend_port,
            [
                {
                    "name": bash_container,
                    "type": "docker",
                    "actions": [{"label": "Terminal", "type": "terminal"}],
                },
            ],
        )

        cleanup_non_vm_cards(page)
        ensure_vm_card_exists(page)
        refresh_vm_card(page)

        # Count existing cards
        initial_count = page.locator("[data-card-id]").count()

        # Click Terminal action button
        action_btn = page.locator(f'[data-vm-action="{bash_container}"][data-action-idx="0"]')
        action_btn.wait_for(state="visible", timeout=5000)
        action_btn.click()

        # Poll until card count increases (10s budget for WS -> backend -> spawn pipeline).
        page.wait_for_function(
            f"() => document.querySelectorAll('[data-card-id]').length > {initial_count}",
            timeout=10000,
        )

        # Verify new card was spawned
        new_count = page.locator("[data-card-id]").count()
        assert new_count > initial_count, "A new card should be spawned after clicking Terminal action"


# ── S6: Status accuracy after external state change ──────────────────────────


class TestRealStatusAccuracy:
    """S6: Status indicators reflect external container state changes."""

    def test_status_updates_after_external_stop(self, page, backend_port, running_container, stopped_container):
        """Stop a running container externally, re-render, verify status flips."""
        # We need a dedicated container for this test since we'll stop it
        name = f"e2e-vm-stoppable-{uuid.uuid4().hex[:8]}"
        subprocess.run(
            [
                "docker.exe",
                "run",
                "-d",
                "--name",
                name,
                "alpine:latest",
                "sleep",
                "3600",
            ],
            check=True,
            capture_output=True,
        )

        try:
            # Add the container to favorites
            set_favorites(
                page,
                backend_port,
                [
                    {
                        "name": name,
                        "type": "docker",
                        "actions": [{"label": "Terminal", "type": "terminal"}],
                    },
                ],
            )

            cleanup_non_vm_cards(page)
            ensure_vm_card_exists(page)
            refresh_vm_card(page)

            # Verify initially Online
            online = page.evaluate(
                """(name) => {
                const spans = document.querySelectorAll('span[title="Online"]');
                for (const s of spans) {
                    if (s.closest('[data-card-id]') && s.parentElement.textContent.includes(name)) {
                        return true;
                    }
                }
                return false;
            }""",
                name,
            )
            assert online, "Container should initially show Online"

            # No Start button while running
            start_btn = page.locator(f'[data-vm-start="{name}"]')
            assert start_btn.count() == 0, "No Start button while container is running"

            # Stop the container externally
            subprocess.run(
                ["docker.exe", "stop", name],
                check=True,
                capture_output=True,
                timeout=15,
            )

            # Re-render to pick up the state change
            refresh_vm_card(page)

            # Verify now Offline
            offline = page.evaluate(
                """(name) => {
                const spans = document.querySelectorAll('span[title="Offline"]');
                for (const s of spans) {
                    if (s.closest('[data-card-id]') && s.parentElement.textContent.includes(name)) {
                        return true;
                    }
                }
                return false;
            }""",
                name,
            )
            assert offline, "Container should show Offline after external stop"

            # Start button should now appear
            start_btn_after = page.locator(f'[data-vm-start="{name}"]')
            assert start_btn_after.count() > 0, "Start button should appear after container is stopped"

        finally:
            subprocess.run(
                ["docker.exe", "rm", "-f", name],
                capture_output=True,
            )
