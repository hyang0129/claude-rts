"""Playwright Electron fixtures for supreme-claudemander smoke tests.

Launches the Python backend with --electron --dev-config stress-test,
then connects Playwright to the Electron window.
"""

import os
import subprocess
import sys
import time

import pytest

# Skip the entire e2e module if playwright is not installed
playwright = pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright  # noqa: E402


def _wait_for_server(port: int, timeout: float = 15.0) -> bool:
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


@pytest.fixture(scope="session")
def backend_port():
    """Return the port for the backend server."""
    return int(os.environ.get("CLAUDE_RTS_PORT", "3099"))


@pytest.fixture(scope="session")
def backend_server(backend_port):
    """Start the backend server for the test session."""
    env = os.environ.copy()
    env["CLAUDE_RTS_TEST_MODE"] = "1"
    # Remove ELECTRON_RUN_AS_NODE so Electron can launch properly
    env.pop("ELECTRON_RUN_AS_NODE", None)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "claude_rts",
            "--port",
            str(backend_port),
            "--no-browser",
            "--dev-config",
            "stress-test",
            "--test-mode",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if not _wait_for_server(backend_port):
        proc.terminate()
        stdout = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        pytest.fail(f"Backend did not start within timeout.\nstdout: {stdout}\nstderr: {stderr}")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def electron_app(backend_server, backend_port):
    """Launch the Electron app via Playwright and yield the first window."""
    import pathlib

    electron_dir = pathlib.Path(__file__).resolve().parents[2] / "electron"
    electron_exe = electron_dir / "node_modules" / "electron" / "dist" / "electron.exe"
    if not electron_exe.exists():
        electron_exe = electron_dir / "node_modules" / "electron" / "dist" / "electron"

    if not electron_exe.exists():
        pytest.skip("Electron not installed — run 'npm install' in electron/")

    headed = os.environ.get("HEADED", "").lower() in ("1", "true")

    pw = sync_playwright().start()

    launch_args = [str(electron_dir), "--port", str(backend_port)]
    if not headed:
        launch_args.insert(0, "--headless")

    app = pw._playwright.electron.launch(
        executable_path=str(electron_exe),
        args=launch_args,
    )

    yield app

    app.close()
    pw.stop()


@pytest.fixture(scope="session")
def page(electron_app, backend_port):
    """Get the first Electron window as a Playwright page."""
    window = electron_app.first_window()
    # Wait for the page to load the backend URL
    window.wait_for_load_state("networkidle")
    # Wait for the canvas element to be present
    window.wait_for_selector("#canvas", timeout=10000)
    return window
