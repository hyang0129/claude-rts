"""Static invariants for epic #236 state-ownership rules.

These tests read ``claude_rts/static/index.html`` as text and assert that
client-side code does not regress the server-owned-state invariants
documented in ``docs/state-model.md``. A failure here means a PR has
re-introduced the "client authoritative state" antipattern (DP-1 / I-1
from epic #236 intent) — the client assigned a server-owned field outside
one of the allowed paths.

Scenarios covered (from issue #250, ``.claude-work/EPIC_236_QA_SCENARIOS.md``
Area 10):

- 10.1 — ``this.starred =`` only in constructor / deserializer /
  ``_applyStarredBroadcast``.
- 10.2 — ``this.{x,y,w,h} =`` only in constructor / ``setupDrag`` /
  ``setupResize`` / ``CARD_UPDATED_FIELD_HANDLERS`` appliers.
- 10.3 — ``saveLayout()`` no longer exists. All textual references are
  explanatory comments, never live calls.
- 10.4 — ``/api/cards/{id}/state`` is only ``fetch``'d from inside
  ``putCardState``; every other writer goes through that helper.

When one of these tests fails, the fix is almost always "move the
assignment into an allowed path" or "stop writing the server-owned field
directly" — not "add the new location to the allowlist below". Adding to
the allowlist requires a documented justification and an update to
``docs/state-model.md``.
"""

from __future__ import annotations

import pathlib
import re


INDEX_HTML = pathlib.Path(__file__).resolve().parent.parent / "claude_rts" / "static" / "index.html"


def _load_source() -> str:
    assert INDEX_HTML.exists(), f"index.html not found at {INDEX_HTML}"
    return INDEX_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Match a full-line comment (leading whitespace then ``//``). We strip these
# from the source before scanning for assignments so we don't flag
# explanatory prose. Inline ``//`` comments after code are kept intact — an
# assignment followed by a comment on the same line is still an assignment.
_LINE_COMMENT_RE = re.compile(r"^\s*//")

# Match function / method declarations so we can attribute each match to the
# enclosing scope. Requires the line to end with ``{`` (optionally followed
# by whitespace) — this differentiates real declarations from identically-
# shaped call sites (``drawMinimap();``) and ``super(...)`` invocations.
# Covers both ``function foo(...)`` and the concise ``foo(...)`` method
# syntax used inside the ``Card`` class. Arrow functions
# (``const onMove = (e) => {``) intentionally do NOT match so the walk
# attributes hits to the outer named method rather than the inner callback.
_FUNC_DECL_RE = re.compile(r"^\s*(?:async\s+)?(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*\{\s*$")


def _lines_non_comment(source: str) -> list[tuple[int, str]]:
    """Return ``(1-based line number, line)`` pairs skipping line-comment-only lines."""
    out: list[tuple[int, str]] = []
    for idx, line in enumerate(source.splitlines(), start=1):
        if _LINE_COMMENT_RE.match(line):
            continue
        out.append((idx, line))
    return out


def _enclosing_function(source_lines: list[str], hit_line_1based: int) -> str | None:
    """Walk backwards from ``hit_line_1based`` to the nearest function/method decl.

    Returns the identifier name, or ``None`` if the scan ran off the top of
    the file without finding a declaration (which would itself be a bug —
    every line of JS in index.html lives inside *something*).
    """
    for idx in range(hit_line_1based - 1, -1, -1):
        m = _FUNC_DECL_RE.match(source_lines[idx])
        if not m:
            continue
        name = m.group(1)
        # Skip JS control-flow keywords that syntactically match the regex
        # but aren't function declarations.
        if name in {"if", "for", "while", "switch", "catch", "return", "function"}:
            continue
        return name
    return None


def _find_hits(pattern: str) -> list[tuple[int, str, str | None]]:
    """Return ``(lineno, line, enclosing_function)`` for each non-comment regex hit."""
    source = _load_source()
    all_lines = source.splitlines()
    regex = re.compile(pattern)
    hits: list[tuple[int, str, str | None]] = []
    for lineno, line in _lines_non_comment(source):
        if regex.search(line):
            enc = _enclosing_function(all_lines, lineno)
            hits.append((lineno, line.rstrip(), enc))
    return hits


def _assert_all_in(
    hits: list[tuple[int, str, str | None]],
    allowed: set[str],
    invariant_name: str,
) -> None:
    """Assert every hit's enclosing function is in ``allowed``.

    Failure message lists every offending hit with line number and function
    name so the reviewer can see exactly where the regression happened.
    """
    offenders = [(ln, ln_text, fn) for ln, ln_text, fn in hits if fn not in allowed]
    if offenders:
        detail = "\n".join(f"  line {ln} in {fn!r}: {ln_text.strip()}" for ln, ln_text, fn in offenders)
        raise AssertionError(
            f"{invariant_name}: found {len(offenders)} assignment(s) outside "
            f"allowed functions {sorted(allowed)}:\n{detail}\n\n"
            f"Fix: move the assignment into an allowed path, or — if the new "
            f"location is legitimately a server-owned-state write path — "
            f"update this test's allowlist AND docs/state-model.md."
        )


# ---------------------------------------------------------------------------
# 10.1 — this.starred =
# ---------------------------------------------------------------------------

# Allowed enclosing functions for ``this.starred =`` assignments:
# * ``constructor`` — default ``false`` at card construction.
# * ``deserialize`` — hydrates the field from a saved descriptor / server
#   payload during boot. The field value still comes from the server; this
#   is not an authoritative local write.
# * ``_applyStarredBroadcast`` — the single applier driven by the
#   ``card_updated`` WS broadcast. Explicitly called out as the only runtime
#   assignment site in the code comment at the method's definition.
_STARRED_ALLOWED = {"constructor", "deserialize", "_applyStarredBroadcast"}


def test_starred_assignments_only_in_allowed_functions():
    hits = _find_hits(r"this\.starred\s*=")
    assert hits, (
        "expected at least one ``this.starred =`` assignment (constructor default) — did the file move or rename?"
    )
    _assert_all_in(hits, _STARRED_ALLOWED, "10.1 this.starred")


# ---------------------------------------------------------------------------
# 10.2 — this.{x,y,w,h} =  and  this.zOrder =
# ---------------------------------------------------------------------------

# Allowed enclosing functions for position / size / z-order assignments:
# * ``constructor`` — default values at card construction.
# * ``setupDrag`` — optimistic local write during a drag gesture. The
#   authoritative commit is the ``pointerup`` PUT to ``/api/cards/{id}/state``.
# * ``setupResize`` — same pattern for width / height.
# * Per-field appliers in ``CARD_UPDATED_FIELD_HANDLERS``: ``x``, ``y``,
#   ``w``, ``h``, ``z_order``. These are the only places that assign to
#   ``card.<field>`` in response to a server broadcast. The appliers use
#   ``card.`` rather than ``this.`` so they don't match this regex, but the
#   per-field handler names do appear in the enclosing-function walk
#   because each applier is itself a named method-shorthand property in the
#   handler-object literal.
_GEOMETRY_ALLOWED = {"constructor", "setupDrag", "setupResize"}


def test_geometry_assignments_only_in_allowed_functions():
    # Covers both plain (``this.x =``) and destructured
    # (``[this.x, this.y] = snapEdges(...)``) assignment forms.
    pattern = r"(this\.(?:x|y|w|h)\s*=|\[this\.[xy]\s*,\s*this\.[xy]\]\s*=)"
    hits = _find_hits(pattern)
    assert hits, "expected at least one geometry assignment (constructor defaults)"
    _assert_all_in(hits, _GEOMETRY_ALLOWED, "10.2 this.{x,y,w,h}")


def test_zorder_assignments_only_in_allowed_functions():
    # ``this.zOrder = nextZ`` lives inside the focus-click handler, which
    # is a ``.addEventListener('click', () => { ... })`` callback — not a
    # named function. So the enclosing-function walk attributes it to the
    # outer method the event is wired in (``setupEl`` / wherever). We keep
    # the allowlist open here to any method named *el*-like OR constructor.
    hits = _find_hits(r"this\.zOrder\s*=")
    assert hits, "expected at least one this.zOrder = assignment"
    # The two known sites:
    #   - constructor (default 0)
    #   - the focus-click arrow callback inside ``createEl`` / similar
    # Capture whichever names the scanner reports, then lock them in so a
    # *new* third site fails the test.
    enclosing_names = {fn for _, _, fn in hits}
    # Defensive floor: the constructor must be among them.
    assert "constructor" in enclosing_names, (
        f"expected ``constructor`` among enclosing functions for this.zOrder, got {sorted(enclosing_names)}"
    )
    # Hard cap: at most 2 distinct enclosing functions. A third would mean
    # a new assignment site slipped in.
    assert len(enclosing_names) <= 2, (
        f"10.2 this.zOrder: expected ≤ 2 distinct enclosing functions "
        f"(constructor + focus-click handler), got {sorted(enclosing_names)}. "
        f"A new assignment site was added — confirm it is on the allowed "
        f"focus-commit path and update this test if so."
    )


# ---------------------------------------------------------------------------
# 10.3 — saveLayout() has been deleted
# ---------------------------------------------------------------------------


def test_savelayout_is_not_defined_or_called():
    """``saveLayout`` must appear only inside line comments.

    Epic #236 child 5 (#241) deleted the function outright. Any non-comment
    occurrence means either a definition crept back in, a live call
    re-emerged, or — worst — a new code path is trying to write the canvas
    JSON from the client.
    """
    source = _load_source()
    bad: list[tuple[int, str]] = []
    for lineno, line in _lines_non_comment(source):
        if "saveLayout" not in line:
            continue
        # Allow inline strings containing the word (none exist today, but
        # don't flag hypothetical future doc strings). We only care about
        # identifier usage: ``saveLayout(`` call or ``function saveLayout``.
        if re.search(r"\bsaveLayout\s*\(", line) or re.search(r"function\s+saveLayout\b", line):
            bad.append((lineno, line.rstrip()))
    if bad:
        detail = "\n".join(f"  line {ln}: {ln_text.strip()}" for ln, ln_text in bad)
        raise AssertionError(
            "10.3 saveLayout: found live reference(s) to saveLayout. It was "
            "deleted by epic #236 child 5 (#241); every mutation must flow "
            "through putCardState() → PUT /api/cards/{id}/state.\n" + detail
        )


# ---------------------------------------------------------------------------
# 10.4 — /api/cards/{id}/state only written through putCardState
# ---------------------------------------------------------------------------


def test_api_cards_state_only_fetched_from_putCardState():
    """Every ``fetch(`/api/cards/...`)`` must be inside ``putCardState``.

    A direct ``fetch`` anywhere else bypasses the single-mutation-path
    invariant (I-1). The helper exists so there is exactly one place to
    instrument, rate-limit, or extend this call.
    """
    source = _load_source()
    all_lines = source.splitlines()
    offenders: list[tuple[int, str, str | None]] = []
    # Allowlist of non-mutation /api/cards/ paths that legitimately bypass the
    # single-mutation helper. These are creation / read endpoints, not state
    # writers. Update this list when adding a new such endpoint and document
    # why it is not state mutation.
    #
    # - ``/api/cards/widget`` — server-authored WidgetCard spawn (epic #254
    #   child 5 / #260). Creates the card; it does not patch existing state.
    NON_MUTATION_PATHS = ("/api/cards/widget",)
    for lineno, line in _lines_non_comment(source):
        if "/api/cards/" not in line:
            continue
        # We only care about ``fetch(...)`` call sites. Matching on
        # ``fetch(`` on the same line as ``/api/cards/`` catches the whole
        # template-literal form used today.
        if "fetch(" not in line:
            continue
        if any(p in line for p in NON_MUTATION_PATHS):
            continue
        enc = _enclosing_function(all_lines, lineno)
        if enc != "putCardState":
            offenders.append((lineno, line.rstrip(), enc))
    if offenders:
        detail = "\n".join(f"  line {ln} in {fn!r}: {ln_text.strip()}" for ln, ln_text, fn in offenders)
        raise AssertionError(
            "10.4 /api/cards/{id}/state: found fetch(...) outside "
            "putCardState. All writers must go through the helper so the "
            "single-mutation-path invariant (I-1) is locally verifiable.\n" + detail
        )


def test_putCardState_is_defined_exactly_once():
    """Sanity floor: ``putCardState`` is the helper — it must exist."""
    source = _load_source()
    matches = re.findall(r"^\s*async\s+function\s+putCardState\s*\(", source, re.M)
    assert len(matches) == 1, (
        f"expected exactly one ``async function putCardState(...)`` declaration; found {len(matches)}"
    )
