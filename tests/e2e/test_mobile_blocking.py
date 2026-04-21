"""E2E tests for the mobile-blocking gate (issue #223).

Verifies that mobile viewports see a "desktop browser required" message and
that the canvas is not rendered, while desktop viewports render normally.

These tests use a dedicated browser context with a mobile-sized viewport to
emulate an iPhone SE.  Unlike the module-scoped ``page`` fixture in
conftest.py, we create fresh contexts here because the existing fixture
uses the default desktop viewport and waits for
``window.__claudeRtsBootComplete`` which is never set on mobile (the boot
IIFE never runs once the body is replaced).
"""

import os

import pytest

pw = pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright  # noqa: E402


@pytest.fixture(scope="module")
def mobile_playwright(backend_server, backend_port):
    """Run a Playwright instance dedicated to viewport-sensitive tests.

    Module-scoped so we don't pay the launch cost per test.
    """
    headed = os.environ.get("HEADED", "").lower() in ("1", "true")
    p = sync_playwright().start()
    browser = p.chromium.launch(headless=not headed)
    yield browser, backend_port
    browser.close()
    p.stop()


def test_mobile_viewport_shows_blocking_message(mobile_playwright):
    """iPhone SE (375px) viewport shows blocking message; canvas is not rendered."""
    browser, port = mobile_playwright
    context = browser.new_context(viewport={"width": 375, "height": 667})
    page = context.new_page()
    try:
        page.goto(f"http://localhost:{port}")
        page.wait_for_selector("#mobile-block", timeout=10000)

        # Blocking message content is visible
        assert page.locator("#mobile-block").is_visible()
        assert "Desktop browser required" in page.inner_text("#mobile-block")
        assert "supreme-claudemander requires a desktop browser" in page.inner_text("#mobile-block")

        # Canvas and status bar should not exist (body was replaced)
        assert page.locator("#canvas").count() == 0
        assert page.locator("#status-bar").count() == 0
    finally:
        context.close()


def test_coarse_pointer_shows_blocking_message(mobile_playwright):
    """Coarse-pointer device (e.g. tablet) shows blocking message even at >=768px wide."""
    browser, port = mobile_playwright
    # 1024px wide but coarse pointer — emulates a tablet
    context = browser.new_context(
        viewport={"width": 1024, "height": 768},
        has_touch=True,
        is_mobile=True,
    )
    page = context.new_page()
    try:
        page.goto(f"http://localhost:{port}")
        page.wait_for_selector("#mobile-block", timeout=10000)
        assert page.locator("#mobile-block").is_visible()
        assert page.locator("#canvas").count() == 0
    finally:
        context.close()


def test_desktop_viewport_renders_normally(mobile_playwright):
    """Desktop viewport (>=768px, fine pointer) renders canvas; no blocking message."""
    browser, port = mobile_playwright
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(f"http://localhost:{port}")
        page.wait_for_selector("#canvas", timeout=15000)
        page.wait_for_function(
            "() => window.__claudeRtsBootComplete === true",
            timeout=15000,
        )

        # Canvas renders; no blocking message
        assert page.locator("#canvas").is_visible()
        assert page.locator("#mobile-block").count() == 0
    finally:
        context.close()


def test_blocking_block_uses_no_user_agent():
    """Scenario 3: the mobile-block script does not reference navigator.userAgent."""
    # Locate index.html relative to this test file
    here = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.normpath(os.path.join(here, "..", "..", "claude_rts", "static", "index.html"))
    with open(index_path, encoding="utf-8") as fh:
        contents = fh.read()

    # Extract the mobile-block script block (self-contained per scenario 4)
    start = contents.find("Mobile-blocking gate (issue #223)")
    assert start != -1, "Mobile-blocking gate comment not found"
    # Take a generous window to capture the entire IIFE
    block = contents[start : start + 2000]
    script_end = block.find("</script>")
    assert script_end != -1, "mobile-block </script> closing tag not found"
    block = block[:script_end]

    assert "userAgent" not in block, "mobile-block script must not reference userAgent"
    assert "navigator" not in block, "mobile-block script must not reference navigator"


def test_blocking_block_is_self_contained_and_short():
    """Scenario 4: blocking logic is a single isolated <script> block under 20 lines of code."""
    here = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.normpath(os.path.join(here, "..", "..", "claude_rts", "static", "index.html"))
    with open(index_path, encoding="utf-8") as fh:
        contents = fh.read()

    start = contents.find("<!-- Mobile-blocking gate (issue #223)")
    assert start != -1
    end = contents.find("</script>", start)
    assert end != -1
    block = contents[start : end + len("</script>")]

    # Count code lines (exclude the HTML comment and blank lines, keep JS lines)
    js_start = block.find("<script>")
    js_end = block.find("</script>")
    js = block[js_start + len("<script>") : js_end]
    code_lines = [ln for ln in js.splitlines() if ln.strip()]
    assert len(code_lines) < 20, f"mobile-block JS has {len(code_lines)} code lines (limit: < 20)"
