"""QA runner — ``python -m claude_rts qa run <scenario-id>`` / ``qa list`` / ``qa verdict``.

The CLI is a dumb runner: it accepts a scenario ID, drives Playwright to the
human gate state, takes a screenshot, and exits.  Verdict posting is a separate
command so the agent can assess the screenshot first and record the human's
judgment after.

GitHub issue comments are the only verdict record.  An agent reads the debt
issue to determine which scenarios are unverified, then calls
``python -m claude_rts qa run <id>`` for each one, reads the screenshot, and
calls ``python -m claude_rts qa verdict <id> <verdict>`` after the human
confirms.  See ``docs/qa-scenarios.md`` for the full agent workflow.

Browser mode
------------
Headless by default — the screenshot is the output artifact.
Override with the ``HEADED`` environment variable to watch via noVNC:
  ``HEADED=1 python -m claude_rts qa run <id>``   → headed (visible in noVNC)
  ``HEADED=0 python -m claude_rts qa run <id>``   → headless (default)
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

_SCENARIOS_DIR = pathlib.Path(__file__).resolve().parent.parent / "qa_scenarios"

_FALLBACK_REPO = "hyang0129/supreme-claudemander"

# Port used by the qa runner backend — distinct from the default 3000 so it
# does not collide with a running production server.
_QA_PORT = 3097

_VERDICT_OPTIONS = ("pass", "fail", "inconclusive", "blocked")
_VERDICT_LABELS = {
    "pass": "PASS ✓",
    "fail": "FAIL ✗",
    "inconclusive": "INCONCLUSIVE ?",
    "blocked": "BLOCKED ⊘",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_repo_slug() -> str:
    """Derive the GitHub repo slug from the git remote origin URL."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        url = result.stdout.strip()
        m = re.match(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
        m = re.match(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
    except Exception:
        pass
    print(
        f"[qa] Warning: could not derive repo slug from git remote; using fallback '{_FALLBACK_REPO}'",
        file=sys.stderr,
    )
    return _FALLBACK_REPO


def _current_commit_sha() -> str:
    """Return the short HEAD commit SHA, or 'unknown' on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        sha = result.stdout.strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def _post_github_comment(repo: str, issue_number: int, body: str) -> bool:
    """Post a comment on the given GitHub issue using the gh CLI.

    Returns True on success, False on failure.
    """
    try:
        result = subprocess.run(
            ["gh", "issue", "comment", str(issue_number), "--repo", repo, "--body", body],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(
                f"[qa] gh issue comment failed (exit {result.returncode}): {result.stderr.strip()}",
                file=sys.stderr,
            )
            return False
        return True
    except FileNotFoundError:
        print(
            "[qa] 'gh' CLI not found — install it from https://cli.github.com/",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(f"[qa] gh issue comment error: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Gate cache — persists gate question/expected between `qa run` and `qa verdict`
# ---------------------------------------------------------------------------


def _gate_cache_path(scenario_id: str) -> pathlib.Path:
    return pathlib.Path(tempfile.gettempdir()) / f"qa-gate-{scenario_id}.json"


def _save_gate_cache(scenario_id: str, gate) -> None:
    data = {
        "scenario_id": gate.scenario_id,
        "question": gate.question,
        "expected": gate.expected,
    }
    _gate_cache_path(scenario_id).write_text(json.dumps(data), encoding="utf-8")


def _load_gate_cache(scenario_id: str):
    """Return a HumanGate loaded from the cache, or None if not found."""
    from claude_rts.qa_scenario import HumanGate

    path = _gate_cache_path(scenario_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return HumanGate(**data)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Screenshot path — deterministic so caller can reference it
# ---------------------------------------------------------------------------


def _screenshot_path(scenario_id: str) -> pathlib.Path:
    return pathlib.Path(tempfile.gettempdir()) / f"qa-screenshot-{scenario_id}.png"


# ---------------------------------------------------------------------------
# Scenario discovery
# ---------------------------------------------------------------------------


def _discover_scenarios() -> list[pathlib.Path]:
    """Return sorted list of scenario Python files in ``qa_scenarios/``."""
    if not _SCENARIOS_DIR.exists():
        return []
    return sorted(_SCENARIOS_DIR.glob("*.py"))


def _load_scenario_class(path: pathlib.Path):
    """Import a scenario file and return its ``Scenario`` class.

    Returns ``None`` with a warning if loading fails.
    """
    spec = importlib.util.spec_from_file_location(f"qa_scenario_{path.stem}", path)
    if spec is None or spec.loader is None:
        print(f"[qa] Warning: could not load scenario from {path}", file=sys.stderr)
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:
        print(f"[qa] Warning: error loading {path}: {exc}", file=sys.stderr)
        return None
    cls = getattr(module, "Scenario", None)
    if cls is None:
        print(f"[qa] Warning: {path} has no 'Scenario' class — skipping", file=sys.stderr)
        return None
    return cls


def _find_scenario_class(scenario_id: str):
    """Find and load the scenario class matching ``scenario_id``.

    Returns ``(cls, available_ids)`` where ``cls`` is ``None`` if not found.
    """
    paths = _discover_scenarios()
    available = []
    for path in paths:
        cls = _load_scenario_class(path)
        if cls is None:
            continue
        sid = getattr(cls, "scenario_id", None)
        if sid is None:
            continue
        available.append(sid)
        if sid == scenario_id:
            return cls, available
    return None, available


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def _wait_for_server(port: int, timeout: float = 30.0) -> bool:
    """Poll until the backend responds on the given port."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    url = f"http://localhost:{port}/api/config"
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def _start_server(
    preset: str, port: int
) -> tuple[subprocess.Popen, tempfile.NamedTemporaryFile, tempfile.NamedTemporaryFile]:  # type: ignore[type-arg]
    """Start the backend server with the given dev-config preset."""
    stdout_file = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, prefix="qa-stdout-")
    stderr_file = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, prefix="qa-stderr-")
    env = os.environ.copy()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "claude_rts",
            "--port",
            str(port),
            "--no-browser",
            "--dev-config",
            preset,
        ],
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
    )
    return proc, stdout_file, stderr_file


def _stop_server(
    proc: subprocess.Popen,  # type: ignore[type-arg]
    stdout_file: tempfile.NamedTemporaryFile,  # type: ignore[type-arg]
    stderr_file: tempfile.NamedTemporaryFile,  # type: ignore[type-arg]
) -> None:
    """Terminate the server process and clean up temp log files."""
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
            pass


# ---------------------------------------------------------------------------
# Playwright runner
# ---------------------------------------------------------------------------


def _run_scenario_with_playwright(scenario_instance, port: int) -> "tuple[HumanGate, str]":  # noqa: F821
    """Launch Playwright, navigate to the app, run the scenario setup, and take a screenshot.

    Returns ``(gate, screenshot_path)`` where ``screenshot_path`` is the saved PNG.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "[qa] Playwright is not installed.\n"
            "Install it with:  pip install -e '.[e2e]' && python -m playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    headed = os.environ.get("HEADED", "0").lower() not in ("0", "false")
    sid = getattr(scenario_instance, "scenario_id", "unknown")
    shot_path = str(_screenshot_path(sid))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        page = browser.new_page()
        page.goto(f"http://localhost:{port}")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#canvas", timeout=15000)
        page.wait_for_function(
            "() => window.__claudeRtsBootComplete === true",
            timeout=15000,
        )

        gate = scenario_instance.run_setup(page)

        page.screenshot(path=shot_path, full_page=False)
        browser.close()

    return gate, shot_path


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def list_scenarios() -> None:
    """Print all discovered scenario IDs and their linked debt issues.

    Called by ``python -m claude_rts qa list``.
    """
    paths = _discover_scenarios()
    if not paths:
        print("[qa] No scenarios found in qa_scenarios/.")
        sys.exit(0)

    rows = []
    for path in paths:
        cls = _load_scenario_class(path)
        if cls is None:
            continue
        sid = getattr(cls, "scenario_id", None)
        if sid is None:
            continue
        issue = getattr(cls, "debt_issue", "?")
        preset = getattr(cls, "preset", "default")
        rows.append((sid, issue, preset))

    if not rows:
        print("[qa] No valid scenarios found.")
        sys.exit(0)

    print(f"{'SCENARIO ID':<45} {'DEBT ISSUE':<12} PRESET")
    print("-" * 70)
    for sid, issue, preset in rows:
        print(f"{sid:<45} #{issue:<11} {preset}")
    sys.exit(0)


def run_scenario(scenario_id: str) -> None:
    """Drive Playwright to the gate state and save a screenshot.

    Prints the gate question, expected state, and screenshot path to stdout.
    Does NOT post a verdict comment — use ``qa verdict`` for that after
    assessing the screenshot.

    Called by ``python -m claude_rts qa run <scenario-id>``.

    Exits 0 on success.
    Exits 1 if the scenario is not found or the server fails to start.
    """
    cls, available = _find_scenario_class(scenario_id)

    if cls is None:
        print(f"[qa] ERROR: scenario '{scenario_id}' not found.", file=sys.stderr)
        if available:
            print(f"[qa] Available scenarios: {', '.join(available)}", file=sys.stderr)
        else:
            print("[qa] No scenarios found in qa_scenarios/.", file=sys.stderr)
        sys.exit(1)

    preset = getattr(cls, "preset", "default")
    debt_issue = getattr(cls, "debt_issue", None)

    print(f"[qa] Running scenario: {scenario_id}")
    print(f"[qa] Preset: {preset}   Debt issue: #{debt_issue}")
    print(f"[qa] Starting server on port {_QA_PORT} (preset={preset}) ...")

    proc, stdout_file, stderr_file = _start_server(preset, _QA_PORT)
    try:
        if not _wait_for_server(_QA_PORT, timeout=30.0):
            _stop_server(proc, stdout_file, stderr_file)
            print("[qa] ERROR: Server did not start within 30 seconds. Aborting.", file=sys.stderr)
            sys.exit(1)

        print(f"[qa] Server ready at http://localhost:{_QA_PORT}")

        scenario_instance = cls()
        gate, shot_path = _run_scenario_with_playwright(scenario_instance, _QA_PORT)

    finally:
        _stop_server(proc, stdout_file, stderr_file)

    _save_gate_cache(scenario_id, gate)

    print("\n" + "=" * 70)
    print("QA GATE STATE REACHED")
    print("=" * 70)
    print(f"\nScenario:    {scenario_id}")
    print(f"Question:    {gate.question}")
    print(f"Expected:    {gate.expected}")
    print(f"Screenshot:  {shot_path}")
    print(
        f"\nAssess the screenshot, then record a verdict:\n"
        f"  python -m claude_rts qa verdict {scenario_id} <pass|fail|inconclusive|blocked>"
        f' [--notes "..."]'
    )
    sys.exit(0)


def post_verdict(scenario_id: str, verdict: str, notes: str = "") -> None:
    """Post a verdict comment to the linked GitHub debt issue.

    ``verdict`` must be one of: pass, fail, inconclusive, blocked.
    ``notes`` is an optional free-form string appended to the comment.

    Called by ``python -m claude_rts qa verdict <scenario-id> <verdict>``.

    Exits 0 on success.
    Exits 1 if the scenario is not found, verdict is invalid, or gh fails.
    """
    if verdict not in _VERDICT_OPTIONS:
        print(
            f"[qa] ERROR: verdict must be one of: {', '.join(_VERDICT_OPTIONS)}",
            file=sys.stderr,
        )
        sys.exit(1)

    cls, available = _find_scenario_class(scenario_id)

    if cls is None:
        print(f"[qa] ERROR: scenario '{scenario_id}' not found.", file=sys.stderr)
        if available:
            print(f"[qa] Available scenarios: {', '.join(available)}", file=sys.stderr)
        sys.exit(1)

    debt_issue = getattr(cls, "debt_issue", None)
    if debt_issue is None:
        print("[qa] ERROR: scenario has no debt_issue — cannot post verdict comment.", file=sys.stderr)
        sys.exit(1)

    gate = _load_gate_cache(scenario_id)

    commit_sha = _current_commit_sha()
    timestamp = datetime.now(timezone.utc).isoformat()
    verdict_label = _VERDICT_LABELS[verdict]

    lines = [
        f"## QA Verdict: {verdict_label}",
        "",
        f"**Scenario:** `{scenario_id}`",
    ]
    if gate is not None:
        lines += [
            f"**Question:** {gate.question}",
            f"**Expected:** {gate.expected}",
        ]
    lines += [
        f"**Verdict:** {verdict}",
    ]
    if notes:
        lines.append(f"**Notes:** {notes}")
    lines += [
        f"**Commit:** `{commit_sha}`",
        f"**Timestamp:** {timestamp}",
        "",
        f"*Recorded by `python -m claude_rts qa verdict {scenario_id} {verdict}`*",
    ]
    body = "\n".join(lines)

    repo = _derive_repo_slug()
    ok = _post_github_comment(repo, debt_issue, body)
    if not ok:
        print(f"[qa] ERROR: could not post verdict comment to {repo}#{debt_issue}", file=sys.stderr)
        sys.exit(1)

    print(f"[qa] Verdict '{verdict}' posted to {repo}#{debt_issue}")
    sys.exit(0)
