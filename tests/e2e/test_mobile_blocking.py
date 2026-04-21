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
    """iPhone SE (375px) viewport shows blocking message; canvas/status bar are hidden."""
    browser, port = mobile_playwright
    context = browser.new_context(viewport={"width": 375, "height": 667})
    page = context.new_page()
    try:
        page.goto(f"http://localhost:{port}", wait_until="domcontentloaded")
        page.wait_for_selector("#mobile-block", state="visible", timeout=10000)

        # Blocking message content is visible
        assert page.locator("#mobile-block").is_visible()
        assert "Desktop browser required" in page.inner_text("#mobile-block")
        assert "supreme-claudemander requires a desktop browser" in page.inner_text("#mobile-block")

        # Canvas and status bar are hidden (CSS display:none via the mobile
        # media query) — the acceptance scenario is about visibility, not
        # DOM presence, so these elements may exist but must not be rendered.
        assert not page.locator("#canvas").is_visible()
        assert not page.locator("#status-bar").is_visible()
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
        page.goto(f"http://localhost:{port}", wait_until="domcontentloaded")
        page.wait_for_selector("#mobile-block", state="visible", timeout=10000)
        assert page.locator("#mobile-block").is_visible()
        assert not page.locator("#canvas").is_visible()
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

        # Canvas renders; blocking message is not visible (the element exists
        # in the DOM for CSS targeting but display:none on desktop viewports).
        assert page.locator("#canvas").is_visible()
        assert not page.locator("#mobile-block").is_visible()
    finally:
        context.close()


def test_blocking_block_uses_no_user_agent():
    """Scenario 3: the mobile-block script does not reference navigator.userAgent."""
    # Locate index.html relative to this test file
    here = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.normpath(os.path.join(here, "..", "..", "claude_rts", "static", "index.html"))
    with open(index_path, encoding="utf-8") as fh:
        contents = fh.read()

    # Extract the mobile-block <script> body only (exclude surrounding HTML comment
    # which may mention "navigator user-agent" as prose explaining the design choice).
    marker = contents.find("Mobile-blocking gate (issue 223)")
    assert marker != -1, "Mobile-blocking gate comment not found"
    comment_end = contents.find("-->", marker)
    assert comment_end != -1
    after_comment = contents[comment_end + len("-->") :]
    script_open = after_comment.find("<script>")
    script_close = after_comment.find("</script>")
    assert script_open != -1 and script_close != -1
    script_body = after_comment[script_open + len("<script>") : script_close]

    assert "userAgent" not in script_body, "mobile-block script must not reference userAgent"
    assert "navigator" not in script_body, "mobile-block script must not reference navigator"


def test_blocking_block_is_self_contained_and_short():
    """Scenario 4: blocking logic is a single isolated <script> block under 20 lines of code."""
    here = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.normpath(os.path.join(here, "..", "..", "claude_rts", "static", "index.html"))
    with open(index_path, encoding="utf-8") as fh:
        contents = fh.read()

    start = contents.find("<!-- Mobile-blocking gate (issue 223)")
    assert start != -1
    # Skip past the HTML comment — the comment text itself contains the word
    # "<script>" as prose, so a naive find() picks that up instead of the real tag.
    comment_end = contents.find("-->", start)
    assert comment_end != -1
    after_comment = contents[comment_end + len("-->") :]
    js_start = after_comment.find("<script>")
    js_end = after_comment.find("</script>")
    assert js_start != -1 and js_end != -1
    js = after_comment[js_start + len("<script>") : js_end]
    code_lines = [ln for ln in js.splitlines() if ln.strip()]
    assert len(code_lines) < 20, f"mobile-block JS has {len(code_lines)} code lines (limit: < 20)"
