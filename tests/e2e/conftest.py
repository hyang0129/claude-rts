"""Playwright fixtures for supreme-claudemander e2e tests.

Launches the Python backend with --dev-config <preset>, then opens the
app in a Chromium browser via Playwright.

The dev-config preset defaults to "stress-test" and can be overridden
per-module by defining a ``dev_config_preset`` fixture.
"""

import os
import subprocess
import sys
import tempfile
import time

import pytest

# Skip the entire e2e module if playwright is not installed
pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright  # noqa: E402


def _wait_for_server(port: int, timeout: float = 30.0) -> bool:
    """Poll until the backend responds on the given port."""
    import urllib.request
    import urllib.error

    deadline = time.monotonic() + timeout
    url = f"http://localhost:{port}/api/config"
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


@pytest.fixture(scope="module")
def dev_config_preset():
    """Override in test modules to use a different dev-config preset."""
    return "stress-test"


@pytest.fixture(scope="module")
def backend_port(dev_config_preset):
    """Return the port for the backend server.

    Each preset gets a deterministic port to avoid collisions when
    pytest-xdist or manual runs overlap.
    """
    base = int(os.environ.get("CLAUDE_RTS_PORT", "3099"))
    # Simple hash so different presets get different ports
    offset = sum(ord(c) for c in dev_config_preset) % 100
    return base + offset


@pytest.fixture(scope="module")
def backend_server(backend_port, dev_config_preset):
    """Start the backend server for the test module."""
    env = os.environ.copy()
    env["CLAUDE_RTS_TEST_MODE"] = "1"

    # Write server output to temp files instead of PIPE to avoid blocking
    # when the pipe buffer fills (the server emits a lot of debug output).
    stdout_file = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, prefix="rts-stdout-")
    stderr_file = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, prefix="rts-stderr-")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "claude_rts",
            "--port",
            str(backend_port),
            "--no-browser",
            "--dev-config",
            dev_config_preset,
            "--test-mode",
        ],
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
    )

    if not _wait_for_server(backend_port, timeout=30.0):
        proc.terminate()
        stdout_file.close()
        stderr_file.close()
        stdout = open(stdout_file.name, errors="replace").read()
        stderr = open(stderr_file.name, errors="replace").read()
        os.unlink(stdout_file.name)
        os.unlink(stderr_file.name)
        pytest.fail(f"Backend did not start within timeout.\nstdout: {stdout}\nstderr: {stderr}")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    for f in (stdout_file, stderr_file):
        try:
            f.close()
            os.unlink(f.name)
        except OSError:
            pass  # Windows may hold the file briefly after process exit


@pytest.fixture(scope="module")
def page(backend_server, backend_port):
    """Launch Chromium and navigate to the backend URL."""
    headed = os.environ.get("HEADED", "").lower() in ("1", "true")

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=not headed)
    pg = browser.new_page()
    pg.goto(f"http://localhost:{backend_port}")
    pg.wait_for_load_state("networkidle")
    pg.wait_for_selector("#canvas", timeout=15000)
    # Give the startup script time to spawn cards
    pg.wait_for_timeout(3000)

    yield pg

    pg.close()
    browser.close()
    pw.stop()
