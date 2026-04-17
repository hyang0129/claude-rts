"""E2E tests for backward compat + unknown-type skip (#126).

Verifies two correctness guarantees the card-type refactor must preserve:

1. Unknown-type skip — a saved canvas containing an unrecognised `type` value
   loads without error; the unknown entry is silently skipped.
2. Pre-type legacy backward compat — a canvas saved before the type-field
   refactor (entries have `hub` but no `type`) still loads and produces the
   correct number of terminal cards.

Fixture JSON files live under tests/e2e/fixtures/.

Run:
    pip install pytest-playwright playwright
    python -m playwright install chromium
    python -m pytest tests/e2e/test_compat.py -v

Headed mode:
    HEADED=1 python -m pytest tests/e2e/test_compat.py -v
"""

import json
import pathlib

import pytest

# Skip module entirely if playwright is not installed
pw = pytest.importorskip("playwright")

# Shared helpers live in conftest (single source of truth — see issue #165).
from tests.e2e.conftest import clear_canvas  # noqa: E402

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


# ── Helpers ──────────────────────────────────────────────────────────────────


def get_card_count(page):
    """Return the current length of the JS cards[] array."""
    return page.evaluate("() => cards.length")


def count_cards_by_type(page, card_type):
    """Count cards where card.type === card_type via JS."""
    return page.evaluate("(t) => cards.filter(c => c.type === t).length", card_type)


def put_canvas(page, canvas_name, payload):
    """PUT a canvas layout via the REST API."""
    payload_json = json.dumps(payload)
    page.evaluate(
        f"""async () => {{
            await fetch('/api/canvases/{canvas_name}', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: {json.dumps(payload_json)},
            }});
        }}"""
    )


# ── Module fixture: use start-claude preset ──────────────────────────────────


@pytest.fixture(scope="module")
def dev_config_preset():
    """Use start-claude preset (has a priority profile + util container)."""
    return "start-claude"


# ── Fixture 1: Unknown-type skip ─────────────────────────────────────────────


class TestUnknownTypeSkip:
    """A canvas with an unrecognised card type loads without error;
    the unknown entry is silently skipped."""

    def test_unknown_type_no_error(self, page):
        """Load canvas with unknown type 'foo'; no JS error thrown."""
        page_errors = []

        def _on_page_error(err):
            page_errors.append(str(err))

        page.on("pageerror", _on_page_error)

        fixture = json.loads((FIXTURES_DIR / "unknown_type_canvas.json").read_text())

        clear_canvas(page)
        put_canvas(page, "compat-unknown-type", fixture)

        page.evaluate("() => switchCanvas('compat-unknown-type')")
        # unknown_type_canvas.json has 2 entries; the 'foo' entry is skipped → 1 card.
        page.wait_for_function("() => cards.length === 1", timeout=5000)

        page.remove_listener("pageerror", _on_page_error)
        assert len(page_errors) == 0, f"Unexpected page error(s): {page_errors}"

    def test_unknown_type_correct_card_count(self, page):
        """Exactly one card rendered (the known widget type)."""
        fixture = json.loads((FIXTURES_DIR / "unknown_type_canvas.json").read_text())

        clear_canvas(page)
        put_canvas(page, "compat-unknown-type-count", fixture)

        page.evaluate("() => switchCanvas('compat-unknown-type-count')")
        page.wait_for_function("() => cards.length === 1", timeout=5000)

        total = get_card_count(page)
        assert total == 1, f"Expected 1 card (unknown type skipped), got {total}"

    def test_unknown_type_no_foo_card(self, page):
        """No card with type 'foo' exists on the canvas."""
        fixture = json.loads((FIXTURES_DIR / "unknown_type_canvas.json").read_text())

        clear_canvas(page)
        put_canvas(page, "compat-unknown-type-foo", fixture)

        page.evaluate("() => switchCanvas('compat-unknown-type-foo')")
        page.wait_for_function("() => cards.length === 1", timeout=5000)

        foo_count = count_cards_by_type(page, "foo")
        assert foo_count == 0, f"Expected 0 cards with type 'foo', got {foo_count}"

    def test_known_card_is_widget(self, page):
        """The surviving card is a widget with widgetType 'system-info'."""
        fixture = json.loads((FIXTURES_DIR / "unknown_type_canvas.json").read_text())

        clear_canvas(page)
        put_canvas(page, "compat-unknown-type-widget", fixture)

        page.evaluate("() => switchCanvas('compat-unknown-type-widget')")
        page.wait_for_function("() => cards.length === 1", timeout=5000)

        widget_count = count_cards_by_type(page, "widget")
        assert widget_count == 1, f"Expected 1 widget card, got {widget_count}"

        widget_type = page.evaluate("() => cards.find(c => c.type === 'widget')?.widgetType")
        assert widget_type == "system-info", f"Expected widgetType 'system-info', got {widget_type!r}"


# ── Fixture 2: Pre-type legacy backward compat ──────────────────────────────


class TestPreTypeLegacyCompat:
    """A canvas saved before the type-field refactor (entries have `hub`
    but no `type`) loads correctly and produces terminal cards."""

    def test_legacy_correct_card_count(self, page):
        """Card count matches fixture entry count (2 entries -> 2 terminal cards)."""
        fixture = json.loads((FIXTURES_DIR / "pre_type_legacy_canvas.json").read_text())

        # Resolve the placeholder hub to a real discovered hub
        hub_info = page.evaluate(
            "() => hubs && hubs.length ? { hub: hubs[0].hub, container: hubs[0].container || '' } : null"
        )
        if hub_info is None:
            pytest.skip("No hubs available to test legacy canvas restore")

        # Patch fixture entries with the real hub
        for entry in fixture["cards"]:
            entry["hub"] = hub_info["hub"]
            entry["container"] = hub_info["container"]

        clear_canvas(page)
        put_canvas(page, "compat-legacy-count", fixture)

        page.evaluate("() => switchCanvas('compat-legacy-count')")
        expected = len(fixture["cards"])
        page.wait_for_function(f"() => cards.length === {expected}", timeout=5000)

        total = get_card_count(page)
        assert total == expected, f"Expected {expected} cards from legacy fixture, got {total}"

    def test_legacy_all_cards_are_terminals(self, page):
        """All restored cards are terminal cards."""
        fixture = json.loads((FIXTURES_DIR / "pre_type_legacy_canvas.json").read_text())

        hub_info = page.evaluate(
            "() => hubs && hubs.length ? { hub: hubs[0].hub, container: hubs[0].container || '' } : null"
        )
        if hub_info is None:
            pytest.skip("No hubs available to test legacy canvas restore")

        for entry in fixture["cards"]:
            entry["hub"] = hub_info["hub"]
            entry["container"] = hub_info["container"]

        clear_canvas(page)
        put_canvas(page, "compat-legacy-type", fixture)

        page.evaluate("() => switchCanvas('compat-legacy-type')")
        expected = len(fixture["cards"])
        page.wait_for_function(f"() => cards.length === {expected}", timeout=5000)

        card_types = page.evaluate("() => cards.map(c => c.type)")
        for i, ct in enumerate(card_types):
            assert ct == "terminal", f"Card {i} should be 'terminal' (legacy entry), got {ct!r}"

    def test_legacy_no_page_errors(self, page):
        """No unhandled page errors during legacy canvas restore."""
        page_errors = []

        def _on_page_error(err):
            page_errors.append(str(err))

        page.on("pageerror", _on_page_error)

        fixture = json.loads((FIXTURES_DIR / "pre_type_legacy_canvas.json").read_text())

        hub_info = page.evaluate(
            "() => hubs && hubs.length ? { hub: hubs[0].hub, container: hubs[0].container || '' } : null"
        )
        if hub_info is None:
            page.remove_listener("pageerror", _on_page_error)
            pytest.skip("No hubs available to test legacy canvas restore")

        for entry in fixture["cards"]:
            entry["hub"] = hub_info["hub"]
            entry["container"] = hub_info["container"]

        clear_canvas(page)
        put_canvas(page, "compat-legacy-errors", fixture)

        page.evaluate("() => switchCanvas('compat-legacy-errors')")
        expected = len(fixture["cards"])
        page.wait_for_function(f"() => cards.length === {expected}", timeout=5000)

        page.remove_listener("pageerror", _on_page_error)
        assert len(page_errors) == 0, f"Unexpected page error(s) during legacy restore: {page_errors}"

    def test_legacy_entries_have_no_type_field(self, page):
        """Verify fixture entries genuinely lack a 'type' field (test hygiene)."""
        fixture = json.loads((FIXTURES_DIR / "pre_type_legacy_canvas.json").read_text())
        for i, entry in enumerate(fixture["cards"]):
            assert "type" not in entry, (
                f"Fixture entry {i} should NOT have a 'type' field (this is a pre-type legacy fixture)"
            )
