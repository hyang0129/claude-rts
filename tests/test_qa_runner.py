"""Unit tests for claude_rts.qa_runner and claude_rts.qa_scenario.

These tests mock Playwright and subprocess so they run without a browser or
running server.  They verify:

- Scenario discovery and ordering (alphabetical)
- Verdict filtering (discharged = yes/no only; skip re-queues)
- Verdict JSONL append
- GitHub comment posting (y/n posts; skip does not)
- Repo slug derivation (https + ssh remotes + fallback)
- Scenario class loading (valid, missing Scenario class, exec error)
- HumanGate and QAScenario protocol
- run_next() exits 0 with "no unverified scenarios" when all discharged
"""

from __future__ import annotations

import json
import pathlib
import textwrap
import unittest.mock as mock

import pytest

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_scenarios_dir(tmp_path, monkeypatch):
    """Redirect _SCENARIOS_DIR to a temp directory."""
    import claude_rts.qa_runner as runner

    monkeypatch.setattr(runner, "_SCENARIOS_DIR", tmp_path / "qa_scenarios")
    (tmp_path / "qa_scenarios").mkdir()
    return tmp_path / "qa_scenarios"


@pytest.fixture()
def tmp_verdicts(tmp_path, monkeypatch):
    """Redirect _VERDICTS_FILE to a temp file."""
    import claude_rts.qa_runner as runner

    verdicts_path = tmp_path / "qa-verdicts.jsonl"
    monkeypatch.setattr(runner, "_VERDICTS_FILE", verdicts_path)
    return verdicts_path


def _write_scenario_file(
    scenarios_dir: pathlib.Path, filename: str, scenario_id: str, debt_issue: int = 221
) -> pathlib.Path:
    """Write a minimal valid scenario Python file."""
    path = scenarios_dir / filename
    path.write_text(
        textwrap.dedent(
            f"""
            from claude_rts.qa_scenario import HumanGate, QAScenario

            class Scenario(QAScenario):
                scenario_id = "{scenario_id}"
                debt_issue = {debt_issue}
                preset = "default"

                def run_setup(self, page) -> HumanGate:
                    return HumanGate(
                        scenario_id=self.scenario_id,
                        question="Test question?",
                        expected="Expected state.",
                    )
            """
        ),
        encoding="utf-8",
    )
    return path


def _write_verdict(verdicts_path: pathlib.Path, scenario_id: str, verdict: str) -> None:
    """Append a verdict record to the verdicts file."""
    record = {
        "scenario_id": scenario_id,
        "commit_sha": "abc1234",
        "verdict": verdict,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "question": "Test question?",
    }
    with verdicts_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# HumanGate and QAScenario protocol
# ---------------------------------------------------------------------------


def test_human_gate_fields():
    from claude_rts.qa_scenario import HumanGate

    gate = HumanGate(scenario_id="s1", question="q?", expected="e")
    assert gate.scenario_id == "s1"
    assert gate.question == "q?"
    assert gate.expected == "e"


def test_qa_scenario_protocol_satisfied():
    from claude_rts.qa_scenario import HumanGate, QAScenario

    class S(QAScenario):
        scenario_id = "test-1"
        debt_issue = 100
        preset = "default"

        def run_setup(self, page) -> HumanGate:
            return HumanGate(scenario_id=self.scenario_id, question="q", expected="e")

    assert isinstance(S(), QAScenario)


# ---------------------------------------------------------------------------
# Scenario discovery and ordering
# ---------------------------------------------------------------------------


def test_discover_scenarios_empty(tmp_scenarios_dir):
    import claude_rts.qa_runner as runner

    assert runner._discover_scenarios() == []


def test_discover_scenarios_sorted(tmp_scenarios_dir):
    import claude_rts.qa_runner as runner

    (tmp_scenarios_dir / "syn-003-z.py").write_text("", encoding="utf-8")
    (tmp_scenarios_dir / "syn-001-a.py").write_text("", encoding="utf-8")
    (tmp_scenarios_dir / "syn-002-m.py").write_text("", encoding="utf-8")

    paths = runner._discover_scenarios()
    names = [p.name for p in paths]
    assert names == ["syn-001-a.py", "syn-002-m.py", "syn-003-z.py"]


def test_discover_scenarios_ignores_non_py(tmp_scenarios_dir):
    import claude_rts.qa_runner as runner

    (tmp_scenarios_dir / "syn-001.py").write_text("", encoding="utf-8")
    (tmp_scenarios_dir / "README.md").write_text("", encoding="utf-8")
    (tmp_scenarios_dir / "data.json").write_text("", encoding="utf-8")

    paths = runner._discover_scenarios()
    assert len(paths) == 1
    assert paths[0].name == "syn-001.py"


# ---------------------------------------------------------------------------
# Scenario loading
# ---------------------------------------------------------------------------


def test_load_scenario_class_valid(tmp_scenarios_dir):
    import claude_rts.qa_runner as runner

    path = _write_scenario_file(tmp_scenarios_dir, "syn-001-test.py", "syn-001-test")
    cls = runner._load_scenario_class(path)
    assert cls is not None
    assert cls.scenario_id == "syn-001-test"
    assert cls.debt_issue == 221
    assert cls.preset == "default"


def test_load_scenario_class_missing_class(tmp_scenarios_dir):
    import claude_rts.qa_runner as runner

    path = tmp_scenarios_dir / "bad.py"
    path.write_text("x = 1\n", encoding="utf-8")
    cls = runner._load_scenario_class(path)
    assert cls is None


def test_load_scenario_class_exec_error(tmp_scenarios_dir):
    import claude_rts.qa_runner as runner

    path = tmp_scenarios_dir / "broken.py"
    path.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    cls = runner._load_scenario_class(path)
    assert cls is None


# ---------------------------------------------------------------------------
# Verdict loading and filtering
# ---------------------------------------------------------------------------


def test_load_verdicts_empty(tmp_verdicts):
    import claude_rts.qa_runner as runner

    assert runner._load_verdicts() == []


def test_load_verdicts_records(tmp_verdicts):
    import claude_rts.qa_runner as runner

    _write_verdict(tmp_verdicts, "s1", "yes")
    _write_verdict(tmp_verdicts, "s2", "no")
    _write_verdict(tmp_verdicts, "s3", "skip")

    records = runner._load_verdicts()
    assert len(records) == 3


def test_discharged_scenario_ids_yes_and_no(tmp_verdicts):
    import claude_rts.qa_runner as runner

    _write_verdict(tmp_verdicts, "s1", "yes")
    _write_verdict(tmp_verdicts, "s2", "no")

    discharged = runner._discharged_scenario_ids(runner._load_verdicts())
    assert discharged == {"s1", "s2"}


def test_skip_does_not_discharge(tmp_verdicts):
    import claude_rts.qa_runner as runner

    _write_verdict(tmp_verdicts, "s1", "skip")
    discharged = runner._discharged_scenario_ids(runner._load_verdicts())
    assert discharged == set()


def test_skip_then_yes_discharges(tmp_verdicts):
    import claude_rts.qa_runner as runner

    _write_verdict(tmp_verdicts, "s1", "skip")
    _write_verdict(tmp_verdicts, "s1", "yes")
    discharged = runner._discharged_scenario_ids(runner._load_verdicts())
    assert "s1" in discharged


# ---------------------------------------------------------------------------
# Verdict append
# ---------------------------------------------------------------------------


def test_append_verdict_creates_file(tmp_verdicts):
    import claude_rts.qa_runner as runner

    assert not tmp_verdicts.exists()
    runner._append_verdict(
        {"scenario_id": "s1", "verdict": "yes", "commit_sha": "abc", "timestamp": "t", "question": "q"}
    )
    assert tmp_verdicts.exists()
    records = runner._load_verdicts()
    assert len(records) == 1
    assert records[0]["scenario_id"] == "s1"


def test_append_verdict_appends(tmp_verdicts):
    import claude_rts.qa_runner as runner

    runner._append_verdict(
        {"scenario_id": "s1", "verdict": "yes", "commit_sha": "abc", "timestamp": "t", "question": "q"}
    )
    runner._append_verdict(
        {"scenario_id": "s2", "verdict": "no", "commit_sha": "def", "timestamp": "t", "question": "q"}
    )
    records = runner._load_verdicts()
    assert len(records) == 2


# ---------------------------------------------------------------------------
# GitHub comment posting
# ---------------------------------------------------------------------------


def test_post_github_comment_calls_gh(monkeypatch):
    import claude_rts.qa_runner as runner

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return mock.Mock(returncode=0, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    ok = runner._post_github_comment("owner/repo", 221, "verdict body")
    assert ok is True
    assert calls[0][:4] == ["gh", "issue", "comment", "221"]
    assert "--repo" in calls[0]
    assert "owner/repo" in calls[0]


def test_post_github_comment_returns_false_on_nonzero(monkeypatch):
    import claude_rts.qa_runner as runner

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: mock.Mock(returncode=1, stderr="error"),
    )
    ok = runner._post_github_comment("owner/repo", 221, "body")
    assert ok is False


def test_post_github_comment_returns_false_when_gh_missing(monkeypatch):
    import claude_rts.qa_runner as runner

    def raise_fnf(*a, **kw):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr("subprocess.run", raise_fnf)
    ok = runner._post_github_comment("owner/repo", 221, "body")
    assert ok is False


# ---------------------------------------------------------------------------
# REPO slug derivation
# ---------------------------------------------------------------------------


def test_derive_repo_slug_https(monkeypatch):
    import claude_rts.qa_runner as runner

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: mock.Mock(returncode=0, stdout="https://github.com/owner/myrepo.git\n"),
    )
    assert runner._derive_repo_slug() == "owner/myrepo"


def test_derive_repo_slug_ssh(monkeypatch):
    import claude_rts.qa_runner as runner

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: mock.Mock(returncode=0, stdout="git@github.com:owner/myrepo.git\n"),
    )
    assert runner._derive_repo_slug() == "owner/myrepo"


def test_derive_repo_slug_fallback(monkeypatch):
    import claude_rts.qa_runner as runner

    def raise_exc(*a, **kw):
        raise OSError("no git")

    monkeypatch.setattr("subprocess.run", raise_exc)
    slug = runner._derive_repo_slug()
    assert slug == runner._FALLBACK_REPO


# ---------------------------------------------------------------------------
# run_next() — no scenarios
# ---------------------------------------------------------------------------


def test_run_next_no_scenarios(tmp_scenarios_dir, tmp_verdicts):
    import claude_rts.qa_runner as runner

    with pytest.raises(SystemExit) as exc_info:
        runner.run_next()
    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# run_next() — all discharged
# ---------------------------------------------------------------------------


def test_run_next_all_discharged(tmp_scenarios_dir, tmp_verdicts):
    import claude_rts.qa_runner as runner

    _write_scenario_file(tmp_scenarios_dir, "syn-001.py", "syn-001")
    _write_verdict(tmp_verdicts, "syn-001", "yes")

    with pytest.raises(SystemExit) as exc_info:
        runner.run_next()
    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Synthetic scenario file — smoke-load
# ---------------------------------------------------------------------------


def test_synthetic_scenario_loads():
    """Verify syn-001-container-terminal.py can be imported without error."""
    import claude_rts.qa_runner as runner

    path = pathlib.Path(__file__).resolve().parent.parent / "qa_scenarios" / "syn-001-container-terminal.py"
    assert path.exists(), f"Scenario file not found: {path}"
    cls = runner._load_scenario_class(path)
    assert cls is not None
    assert cls.scenario_id == "syn-001-container-terminal"
    assert cls.debt_issue == 221
    assert cls.preset == "default"


def test_synthetic_scenario_run_setup_returns_human_gate(monkeypatch):
    """run_setup should return a HumanGate when given a mock page."""
    import claude_rts.qa_runner as runner
    from claude_rts.qa_scenario import HumanGate

    path = pathlib.Path(__file__).resolve().parent.parent / "qa_scenarios" / "syn-001-container-terminal.py"
    cls = runner._load_scenario_class(path)
    assert cls is not None

    # Build a minimal mock Playwright page.
    mock_page = mock.Mock()
    mock_page.evaluate.return_value = {"ok": True, "card_id": "card-abc"}

    instance = cls()
    gate = instance.run_setup(mock_page)

    assert isinstance(gate, HumanGate)
    assert gate.scenario_id == "syn-001-container-terminal"
    assert len(gate.question) > 10
    assert len(gate.expected) > 10
