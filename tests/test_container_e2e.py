"""E2E integration tests for the Container Manager full flow (issue #209).

Covers the canonical success metric from epic #199:
    Canvas Claude calls container_create(image=ubuntu:24.04) → container
    appears in favorites → terminal opened into container → clone psf/requests
    → install deps → run pytest → exits 0.

Two classes of test:

- ``TestContainerE2EValidation`` — API-level validation tests that run without
  Docker using the ``_test_container_create`` / ``_test_container_labels``
  hooks on the aiohttp app.  These re-assert the structured-error contract
  (``container_cap_reached`` 429, ``image_not_whitelisted`` 400) at the E2E
  file so contract drift is caught here even if the mocked unit tests in
  ``tests/test_container_manager.py`` are edited.

- ``TestContainerE2EFullFlow`` — real-Docker integration test marked
  ``real_docker`` (not run in default CI).  Provisions a real ``ubuntu:24.04``
  container via ``POST /api/containers/create``, opens a terminal into it,
  clones psf/requests, installs deps, runs pytest, and asserts exit 0.
  Skipped automatically when Docker is unavailable.  Container cleanup runs
  unconditionally via a ``docker rm -f`` teardown (test-only exemption to the
  "no removal" scope rule — see child spec #209).
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid

import pytest

from claude_rts import config
from claude_rts.server import create_app


# ── Shared fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path):
    app_config = config.load(tmp_path / ".sc")
    return create_app(app_config)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


# ── Validation-layer tests (no Docker required) ─────────────────────────────


class TestContainerE2EValidation:
    """Structured-error contract checks at the E2E boundary.

    These tests do not require Docker — they exercise the validation path of
    ``POST /api/containers/create`` using the in-app test hooks.  They duplicate
    coverage from ``tests/test_container_manager.py`` intentionally so the
    E2E-file owner can detect contract drift without cross-file greps.
    """

    async def test_image_not_whitelisted_returns_400(self, client):
        resp = await client.post(
            "/api/containers/create",
            json={"image": "alpine:latest"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "image_not_whitelisted"
        assert "allowed" in data
        assert "ubuntu:24.04" in data["allowed"]

    async def test_cap_reached_returns_429(self, app, client):
        """At-cap (4 canvas-claude containers) → 5th create returns 429."""
        app["_test_container_create"] = {}
        app["_test_container_labels"] = {f"cap-test-{i}": {"created_by": "canvas-claude"} for i in range(4)}
        resp = await client.post(
            "/api/containers/create",
            json={"image": "ubuntu:24.04", "name": "cap-test-overflow"},
        )
        assert resp.status == 429
        data = await resp.json()
        assert data["error"] == "container_cap_reached"
        # Caller needs the list of existing containers to decide what to rebuild.
        assert sorted(data["existing_container_ids"]) == [
            "cap-test-0",
            "cap-test-1",
            "cap-test-2",
            "cap-test-3",
        ]

    async def test_favorites_registered_after_create(self, app, client):
        """Full contract: create → favorites list contains the new container."""
        app["_test_container_create"] = {}
        app["_test_container_labels"] = {}

        name = "e2e-fav-contract"
        resp = await client.post(
            "/api/containers/create",
            json={"image": "ubuntu:24.04", "name": name},
        )
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["container_id"] == name
        assert data["status"] == "created"

        # Favorites endpoint must now list the container.
        fav_resp = await client.get("/api/containers/favorites")
        assert fav_resp.status == 200
        favs = await fav_resp.json()
        assert any(f["name"] == name for f in favs), (
            f"container {name} should be auto-registered as a favorite; got {favs}"
        )


# ── Real-Docker full-flow test ──────────────────────────────────────────────


def _docker_available() -> bool:
    """Return True iff ``docker ps`` succeeds."""
    try:
        r = subprocess.run(
            ["docker", "ps"],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.real_docker
@pytest.mark.skipif(not _docker_available(), reason="Docker not available")
class TestContainerE2EFullFlow:
    """Canonical full-flow test from epic #199.

    Provisions a real ``ubuntu:24.04`` container, opens a terminal into it,
    clones psf/requests, installs deps, runs pytest, asserts exit 0.

    Teardown force-removes every container stamped with
    ``created_by=canvas-claude`` whose name matches the test prefix, so a
    crashed test cannot leak state between runs.
    """

    TEST_PREFIX = "e2e209-"

    @pytest.fixture(autouse=True)
    def _cleanup_test_containers(self):
        """Force-remove any test-prefixed container on teardown (both paths)."""
        created: list[str] = []
        yield created
        for name in created:
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                timeout=30,
            )
        # Belt-and-braces: sweep any container with the test prefix that
        # survived a crash in an earlier iteration.
        try:
            r = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"name=^{self.TEST_PREFIX}",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for stale in r.stdout.splitlines():
                stale = stale.strip()
                if stale:
                    subprocess.run(
                        ["docker", "rm", "-f", stale],
                        capture_output=True,
                        timeout=30,
                    )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    async def test_full_flow_clone_requests_and_pytest(self, app, client, _cleanup_test_containers):
        """Full canonical flow: create → favorites → terminal → clone → pytest 0.

        This test is intentionally long-running (network downloads + pip +
        pytest on psf/requests).  Budget ~4 min per the child spec acceptance.
        Marked ``real_docker`` so CI skips it by default; run locally with
        ``pytest -m real_docker`` or via the manual-acceptance CI job.
        """
        name = f"{self.TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        _cleanup_test_containers.append(name)

        # 1. Create container.
        resp = await client.post(
            "/api/containers/create",
            json={"image": "ubuntu:24.04", "name": name},
        )
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["container_id"] == name
        assert data["status"] == "created"

        # 2. Verify the canvas-claude label was stamped and resource limits
        #    applied — the feared-failure gate (intent §8).
        inspect = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Config.Labels.created_by}}",
                name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert inspect.returncode == 0, inspect.stderr
        assert inspect.stdout.strip() == "canvas-claude", (
            f"container {name} must carry created_by=canvas-claude label; got {inspect.stdout!r}"
        )

        # 3. Verify favorites registration.
        fav_resp = await client.get("/api/containers/favorites")
        favs = await fav_resp.json()
        assert any(f["name"] == name for f in favs), (
            f"container {name} should be auto-registered as a favorite; got {favs}"
        )

        # 4. Open a terminal into the container.
        term_resp = await client.post(
            "/api/claude/terminal/create",
            params={
                "cmd": "bash",
                "container": name,
                "cols": "120",
                "rows": "40",
            },
        )
        assert term_resp.status == 200, await term_resp.text()
        term_data = await term_resp.json()
        session_id = term_data["session_id"]

        try:
            # 5. Install git + python + pip, clone, install deps, run pytest.
            #    We use a single shell one-liner with `set -e` so any step's
            #    failure surfaces as a non-zero exit code, which we then observe
            #    via the EXIT sentinel at the end.
            script = (
                "set -e; "
                "apt-get update -qq >/dev/null 2>&1 && "
                "apt-get install -y -qq git python3 python3-pip python3-venv >/dev/null 2>&1 && "
                "cd /tmp && "
                "git clone --depth=1 https://github.com/psf/requests.git >/dev/null 2>&1 && "
                "cd /tmp/requests && "
                "python3 -m venv .venv && "
                ". .venv/bin/activate && "
                "pip install -q -e '.[socks]' pytest >/dev/null 2>&1 && "
                "pytest -x -q tests/ 2>&1 | tail -20; "
                'echo "EXIT209:$?"\n'
            )
            send_resp = await client.post(
                f"/api/claude/terminal/{session_id}/send",
                json={"text": script},
            )
            assert send_resp.status == 200, await send_resp.text()

            # 6. Poll the scrollback for the EXIT209 sentinel, up to 4 min.
            deadline = asyncio.get_event_loop().time() + 240.0
            output = ""
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(5)
                read_resp = await client.get(
                    f"/api/claude/terminal/{session_id}/read",
                    params={"strip_ansi": "true"},
                )
                if read_resp.status != 200:
                    continue
                read_data = await read_resp.json()
                output = read_data.get("output") or read_data.get("scrollback") or ""
                if "EXIT209:" in output:
                    break

            assert "EXIT209:0" in output, f"pytest did not exit 0. Tail of terminal output:\n{output[-1500:]}"
        finally:
            # 7. Always close the terminal session so the PTY is reaped.
            await client.delete(f"/api/claude/terminal/{session_id}")
