"""QA runner — ``python -m claude_rts qa next``.

Discovers authored scenarios in ``qa_scenarios/``, filters out already-verdicted
ones, launches the app, drives Playwright to the human gate, reads y/n/s, and
records the verdict locally and as a GitHub comment on the linked debt issue.

Verdict storage
---------------
``~/.supreme-claudemander/qa-verdicts.jsonl`` — one JSON record per line:

    {"scenario_id": "syn-001-...", "commit_sha": "abc1234", "verdict": "yes",
     "timestamp": "2026-04-27T16:00:00Z", "question": "Does the terminal appear?"}

A scenario is considered "discharged" when it has a record with
``verdict in {"yes", "no"}`` for **any** commit SHA.  Skipped (``"skip"``) verdicts
are stored for audit/ordering but do not discharge the item — the scenario
re-appears on the next ``qa next`` invocation.

Ordering
--------
Scenarios are sorted alphabetically by filename.  The naming convention
``<prefix>-<NNN>-<slug>.py`` naturally encodes priority order.

Browser mode
------------
``qa next`` launches a headed (visible) browser by default so the human can see
the app state at the gate.  Override with the ``HEADED`` environment variable:
  ``HEADED=0 python -m claude_rts qa next``   → headless (useful for wiring tests)
  ``HEADED=1 python -m claude_rts qa next``   → headed (default)
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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

_VERDICTS_FILE = pathlib.Path.home() / ".supreme-claudemander" / "qa-verdicts.jsonl"
_SCENARIOS_DIR = pathlib.Path(__file__).resolve().parent.parent / "qa_scenarios"

_FALLBACK_REPO = "hyang0129/supreme-claudemander"

# Port used by the qa next backend — distinct from the default 3000 so it
# does not collide with a running production server.
_QA_PORT = 3097


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_repo_slug() -> str:
    """Derive the GitHub repo slug from the git remote origin URL.

    Falls back to ``hyang0129/supreme-claudemander`` with a warning if the
    remote URL cannot be parsed.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        url = result.stdout.strip()
        # HTTPS: https://github.com/owner/repo.git
        m = re.match(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
        # SSH: git@github.com:owner/repo.git
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


def _load_verdicts() -> list[dict]:
    """Load all verdict records from the verdicts JSONL file."""
    if not _VERDICTS_FILE.exists():
        return []
    records = []
    with _VERDICTS_FILE.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def _discharged_scenario_ids(verdicts: list[dict]) -> set[str]:
    """Return the set of scenario IDs that have been discharged (yes or no verdict)."""
    return {r["scenario_id"] for r in verdicts if r.get("verdict") in {"yes", "no"}}


def _append_verdict(record: dict) -> None:
    """Append a single verdict record to the verdicts JSONL file."""
    _VERDICTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _VERDICTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


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
                f"[qa] Warning: gh issue comment failed (exit {result.returncode}): {result.stderr.strip()}",
                file=sys.stderr,
            )
            return False
        return True
    except FileNotFoundError:
        print(
            "[qa] Warning: 'gh' CLI not found — verdict comment not posted to GitHub.",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(f"[qa] Warning: gh issue comment error: {exc}", file=sys.stderr)
        return False


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

    The file must define a class named ``Scenario`` that implements
    ``QAScenario``.  Returns ``None`` with a warning if loading fails.
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
    """Start the backend server with the given dev-config preset.

    Returns ``(proc, stdout_file, stderr_file)``.  Caller must terminate the
    process and clean up the temp files.
    """
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
# Gate I/O
# ---------------------------------------------------------------------------


def _read_single_keystroke() -> str:
    """Read a single keystroke from stdin without requiring Enter.

    Falls back to ``input()`` if the terminal is not a TTY (e.g. tests).
    """
    if not sys.stdin.isatty():
        line = sys.stdin.readline().strip().lower()
        return line[0] if line else ""

    import tty
    import termios

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch.lower()


def _print_gate(gate) -> None:  # gate: HumanGate
    """Print the human gate prompt to stdout."""
    print("\n" + "=" * 70)
    print("HUMAN JUDGMENT GATE")
    print("=" * 70)
    print(f"\nQuestion: {gate.question}")
    print(f"Expected: {gate.expected}")
    print("\nAnswer: [y]es  [n]o  [s]kip")
    print("(Press a single key — no Enter needed)")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Playwright runner
# ---------------------------------------------------------------------------


def _run_scenario_with_playwright(scenario_instance, port: int) -> "HumanGate":  # noqa: F821
    """Launch Playwright, navigate to the app, and run the scenario setup.

    Returns the ``HumanGate`` from ``run_setup()``.
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

    headed = os.environ.get("HEADED", "1").lower() not in ("0", "false")

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

        # Keep the browser open so the human can see the state.
        # The gate prompt is printed AFTER setup completes.
        _print_gate(gate)

        ch = _read_single_keystroke()
        print(f"\nYou answered: {ch!r}")
        sys.stdout.flush()

        browser.close()

    return gate, ch


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_next() -> None:
    """Execute the next unverified scenario.

    Called by ``python -m claude_rts qa next``.
    """
    scenario_paths = _discover_scenarios()
    if not scenario_paths:
        print("[qa] No scenarios found in qa_scenarios/. Nothing to run.")
        sys.exit(0)

    verdicts = _load_verdicts()
    discharged = _discharged_scenario_ids(verdicts)

    # Find the first unverified scenario (alphabetical order, skip discharged).
    next_path = None
    next_cls = None
    for path in scenario_paths:
        cls = _load_scenario_class(path)
        if cls is None:
            continue
        sid = getattr(cls, "scenario_id", None)
        if sid is None:
            print(f"[qa] Warning: {path} Scenario missing scenario_id — skipping", file=sys.stderr)
            continue
        if sid not in discharged:
            next_path = path
            next_cls = cls
            break

    if next_path is None:
        print("[qa] No unverified scenarios. All scenarios have been discharged. Exiting.")
        sys.exit(0)

    preset = getattr(next_cls, "preset", "default")
    debt_issue = getattr(next_cls, "debt_issue", None)
    scenario_id = next_cls.scenario_id

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

        scenario_instance = next_cls()
        gate, ch = _run_scenario_with_playwright(scenario_instance, _QA_PORT)

    finally:
        _stop_server(proc, stdout_file, stderr_file)

    # Map keystroke to verdict
    verdict_map = {"y": "yes", "n": "no", "s": "skip"}
    verdict = verdict_map.get(ch)
    if verdict is None:
        print(f"[qa] Unrecognised key '{ch}' — treating as skip.")
        verdict = "skip"

    commit_sha = _current_commit_sha()
    timestamp = datetime.now(timezone.utc).isoformat()

    record = {
        "scenario_id": scenario_id,
        "commit_sha": commit_sha,
        "verdict": verdict,
        "timestamp": timestamp,
        "question": gate.question,
    }
    _append_verdict(record)
    print(f"[qa] Verdict '{verdict}' saved to {_VERDICTS_FILE}")

    if verdict in {"yes", "no"} and debt_issue is not None:
        repo = _derive_repo_slug()
        verdict_label = "PASS ✓" if verdict == "yes" else "FAIL ✗"
        body = (
            f"## QA Verdict: {verdict_label}\n\n"
            f"**Scenario:** `{scenario_id}`\n"
            f"**Question:** {gate.question}\n"
            f"**Expected:** {gate.expected}\n"
            f"**Verdict:** {verdict}\n"
            f"**Commit:** `{commit_sha}`\n"
            f"**Timestamp:** {timestamp}\n\n"
            f"*Recorded by `python -m claude_rts qa next`*"
        )
        ok = _post_github_comment(repo, debt_issue, body)
        if ok:
            print(f"[qa] Verdict comment posted to {repo}#{debt_issue}")
        else:
            print(f"[qa] Warning: could not post verdict comment to {repo}#{debt_issue}", file=sys.stderr)
    elif verdict == "skip":
        print("[qa] Skipped — no GitHub comment posted. Scenario will re-appear next run.")
