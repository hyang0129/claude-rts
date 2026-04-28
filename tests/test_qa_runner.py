"""Unit tests for claude_rts.qa_runner and claude_rts.qa_scenario.

These tests mock Playwright and subprocess so they run without a browser or
running server.  They verify:

- Scenario discovery and ordering (alphabetical)
- Scenario class loading (valid, missing Scenario class, exec error)
- GitHub comment posting (success, gh failure, gh missing)
- Repo slug derivation (https + ssh remotes + fallback)
- list_scenarios() output
- run_scenario() not-found exits 1 with available IDs
- run_scenario() success saves gate cache and exits 0
- post_verdict() valid verdicts post structured GitHub comments
- post_verdict() invalid verdict exits 1
- post_verdict() gh failure exits 1
- post_verdict() includes notes when provided
- Gate cache round-trip (_save_gate_cache / _load_gate_cache)
- HumanGate and QAScenario protocol
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
# Gate cache
# ---------------------------------------------------------------------------


def test_gate_cache_round_trip(tmp_path, monkeypatch):
    import claude_rts.qa_runner as runner
    from claude_rts.qa_scenario import HumanGate

    monkeypatch.setattr(runner, "_gate_cache_path", lambda sid: tmp_path / f"qa-gate-{sid}.json")

    gate = HumanGate(scenario_id="s1", question="q?", expected="e")
    runner._save_gate_cache("s1", gate)

    loaded = runner._load_gate_cache("s1")
    assert loaded is not None
    assert loaded.scenario_id == "s1"
    assert loaded.question == "q?"
    assert loaded.expected == "e"


def test_gate_cache_missing_returns_none(tmp_path, monkeypatch):
    import claude_rts.qa_runner as runner

    monkeypatch.setattr(runner, "_gate_cache_path", lambda sid: tmp_path / f"qa-gate-{sid}.json")
    assert runner._load_gate_cache("nonexistent") is None


# ---------------------------------------------------------------------------
# list_scenarios()
# ---------------------------------------------------------------------------


def test_list_scenarios_empty(tmp_scenarios_dir):
    import claude_rts.qa_runner as runner

    with pytest.raises(SystemExit) as exc_info:
        runner.list_scenarios()
    assert exc_info.value.code == 0


def test_list_scenarios_prints_ids(tmp_scenarios_dir, capsys):
    import claude_rts.qa_runner as runner

    _write_scenario_file(tmp_scenarios_dir, "syn-001-alpha.py", "syn-001-alpha", debt_issue=100)
    _write_scenario_file(tmp_scenarios_dir, "syn-002-beta.py", "syn-002-beta", debt_issue=200)

    with pytest.raises(SystemExit):
        runner.list_scenarios()

    out = capsys.readouterr().out
    assert "syn-001-alpha" in out
    assert "syn-002-beta" in out
    assert "#100" in out
    assert "#200" in out


# ---------------------------------------------------------------------------
# run_scenario() — not found
# ---------------------------------------------------------------------------


def test_run_scenario_not_found_exits_1(tmp_scenarios_dir):
    import claude_rts.qa_runner as runner

    _write_scenario_file(tmp_scenarios_dir, "syn-001-real.py", "syn-001-real")

    with pytest.raises(SystemExit) as exc_info:
        runner.run_scenario("does-not-exist")
    assert exc_info.value.code == 1


def test_run_scenario_not_found_lists_available(tmp_scenarios_dir, capsys):
    import claude_rts.qa_runner as runner

    _write_scenario_file(tmp_scenarios_dir, "syn-001-real.py", "syn-001-real")

    with pytest.raises(SystemExit):
        runner.run_scenario("does-not-exist")

    err = capsys.readouterr().err
    assert "syn-001-real" in err


# ---------------------------------------------------------------------------
# run_scenario() — success saves gate cache and exits 0
# ---------------------------------------------------------------------------


def test_run_scenario_success_exits_0(tmp_scenarios_dir, tmp_path, monkeypatch):
    """Successful run saves gate cache and exits 0."""
    import claude_rts.qa_runner as runner
    from claude_rts.qa_scenario import HumanGate

    _write_scenario_file(tmp_scenarios_dir, "syn-001-ok.py", "syn-001-ok", debt_issue=221)

    monkeypatch.setattr(runner, "_start_server", lambda *a: (mock.Mock(), mock.Mock(), mock.Mock()))
    monkeypatch.setattr(runner, "_wait_for_server", lambda *a, **kw: True)
    monkeypatch.setattr(runner, "_stop_server", lambda *a: None)
    monkeypatch.setattr(runner, "_gate_cache_path", lambda sid: tmp_path / f"qa-gate-{sid}.json")

    gate = HumanGate(scenario_id="syn-001-ok", question="Does it look right?", expected="Yes.")
    shot = str(tmp_path / "screenshot.png")
    monkeypatch.setattr(runner, "_run_scenario_with_playwright", lambda inst, port: (gate, shot))

    with pytest.raises(SystemExit) as exc_info:
        runner.run_scenario("syn-001-ok")

    assert exc_info.value.code == 0


def test_run_scenario_saves_gate_cache(tmp_scenarios_dir, tmp_path, monkeypatch, capsys):
    """Gate question and expected are saved to the cache file."""
    import claude_rts.qa_runner as runner
    from claude_rts.qa_scenario import HumanGate

    _write_scenario_file(tmp_scenarios_dir, "syn-001-cache.py", "syn-001-cache", debt_issue=221)

    monkeypatch.setattr(runner, "_start_server", lambda *a: (mock.Mock(), mock.Mock(), mock.Mock()))
    monkeypatch.setattr(runner, "_wait_for_server", lambda *a, **kw: True)
    monkeypatch.setattr(runner, "_stop_server", lambda *a: None)

    cache_path = tmp_path / "qa-gate-syn-001-cache.json"
    monkeypatch.setattr(runner, "_gate_cache_path", lambda sid: cache_path)

    gate = HumanGate(scenario_id="syn-001-cache", question="Visible?", expected="Yes visible.")
    shot = str(tmp_path / "shot.png")
    monkeypatch.setattr(runner, "_run_scenario_with_playwright", lambda inst, port: (gate, shot))

    with pytest.raises(SystemExit):
        runner.run_scenario("syn-001-cache")

    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert data["question"] == "Visible?"
    assert data["expected"] == "Yes visible."


def test_run_scenario_prints_screenshot_path(tmp_scenarios_dir, tmp_path, monkeypatch, capsys):
    """Screenshot path is printed to stdout."""
    import claude_rts.qa_runner as runner
    from claude_rts.qa_scenario import HumanGate

    _write_scenario_file(tmp_scenarios_dir, "syn-001-shot.py", "syn-001-shot", debt_issue=221)

    monkeypatch.setattr(runner, "_start_server", lambda *a: (mock.Mock(), mock.Mock(), mock.Mock()))
    monkeypatch.setattr(runner, "_wait_for_server", lambda *a, **kw: True)
    monkeypatch.setattr(runner, "_stop_server", lambda *a: None)
    monkeypatch.setattr(runner, "_gate_cache_path", lambda sid: tmp_path / f"qa-gate-{sid}.json")

    gate = HumanGate(scenario_id="syn-001-shot", question="q?", expected="e")
    shot = "/tmp/qa-screenshot-syn-001-shot.png"
    monkeypatch.setattr(runner, "_run_scenario_with_playwright", lambda inst, port: (gate, shot))

    with pytest.raises(SystemExit):
        runner.run_scenario("syn-001-shot")

    out = capsys.readouterr().out
    assert shot in out


def test_run_scenario_server_fail_exits_1(tmp_scenarios_dir, monkeypatch):
    """If server doesn't start, exit 1."""
    import claude_rts.qa_runner as runner

    _write_scenario_file(tmp_scenarios_dir, "syn-001-sfail.py", "syn-001-sfail")

    monkeypatch.setattr(runner, "_start_server", lambda *a: (mock.Mock(), mock.Mock(), mock.Mock()))
    monkeypatch.setattr(runner, "_wait_for_server", lambda *a, **kw: False)
    monkeypatch.setattr(runner, "_stop_server", lambda *a: None)

    with pytest.raises(SystemExit) as exc_info:
        runner.run_scenario("syn-001-sfail")

    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# post_verdict() — valid verdicts
# ---------------------------------------------------------------------------


def test_post_verdict_pass_posts_comment(tmp_scenarios_dir, tmp_path, monkeypatch):
    """pass verdict posts PASS comment and exits 0."""
    import claude_rts.qa_runner as runner
    from claude_rts.qa_scenario import HumanGate

    _write_scenario_file(tmp_scenarios_dir, "syn-001-v.py", "syn-001-v", debt_issue=221)

    comment_calls = []
    monkeypatch.setattr(runner, "_post_github_comment", lambda r, i, b: comment_calls.append((r, i, b)) or True)
    monkeypatch.setattr(runner, "_derive_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(runner, "_current_commit_sha", lambda: "abc1234")

    gate = HumanGate(scenario_id="syn-001-v", question="Is it right?", expected="Yes.")
    monkeypatch.setattr(runner, "_gate_cache_path", lambda sid: tmp_path / f"qa-gate-{sid}.json")
    runner._save_gate_cache("syn-001-v", gate)

    with pytest.raises(SystemExit) as exc_info:
        runner.post_verdict("syn-001-v", "pass")

    assert exc_info.value.code == 0
    assert len(comment_calls) == 1
    repo, issue, body = comment_calls[0]
    assert issue == 221
    assert "PASS" in body
    assert "syn-001-v" in body
    assert "abc1234" in body


def test_post_verdict_fail_posts_fail_comment(tmp_scenarios_dir, tmp_path, monkeypatch):
    """fail verdict posts FAIL comment."""
    import claude_rts.qa_runner as runner

    _write_scenario_file(tmp_scenarios_dir, "syn-001-f.py", "syn-001-f", debt_issue=221)

    bodies = []
    monkeypatch.setattr(runner, "_post_github_comment", lambda r, i, b: bodies.append(b) or True)
    monkeypatch.setattr(runner, "_derive_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(runner, "_current_commit_sha", lambda: "abc1234")
    monkeypatch.setattr(runner, "_gate_cache_path", lambda sid: tmp_path / f"no-cache-{sid}.json")

    with pytest.raises(SystemExit) as exc_info:
        runner.post_verdict("syn-001-f", "fail")

    assert exc_info.value.code == 0
    assert "FAIL" in bodies[0]


def test_post_verdict_inconclusive(tmp_scenarios_dir, tmp_path, monkeypatch):
    """inconclusive verdict posts INCONCLUSIVE comment."""
    import claude_rts.qa_runner as runner

    _write_scenario_file(tmp_scenarios_dir, "syn-001-i.py", "syn-001-i", debt_issue=221)

    bodies = []
    monkeypatch.setattr(runner, "_post_github_comment", lambda r, i, b: bodies.append(b) or True)
    monkeypatch.setattr(runner, "_derive_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(runner, "_current_commit_sha", lambda: "abc1234")
    monkeypatch.setattr(runner, "_gate_cache_path", lambda sid: tmp_path / f"no-cache-{sid}.json")

    with pytest.raises(SystemExit) as exc_info:
        runner.post_verdict("syn-001-i", "inconclusive")

    assert exc_info.value.code == 0
    assert "INCONCLUSIVE" in bodies[0]


def test_post_verdict_blocked(tmp_scenarios_dir, tmp_path, monkeypatch):
    """blocked verdict posts BLOCKED comment."""
    import claude_rts.qa_runner as runner

    _write_scenario_file(tmp_scenarios_dir, "syn-001-b.py", "syn-001-b", debt_issue=221)

    bodies = []
    monkeypatch.setattr(runner, "_post_github_comment", lambda r, i, b: bodies.append(b) or True)
    monkeypatch.setattr(runner, "_derive_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(runner, "_current_commit_sha", lambda: "abc1234")
    monkeypatch.setattr(runner, "_gate_cache_path", lambda sid: tmp_path / f"no-cache-{sid}.json")

    with pytest.raises(SystemExit) as exc_info:
        runner.post_verdict("syn-001-b", "blocked")

    assert exc_info.value.code == 0
    assert "BLOCKED" in bodies[0]


def test_post_verdict_includes_notes(tmp_scenarios_dir, tmp_path, monkeypatch):
    """Notes are included in the posted comment body."""
    import claude_rts.qa_runner as runner

    _write_scenario_file(tmp_scenarios_dir, "syn-001-n.py", "syn-001-n", debt_issue=221)

    bodies = []
    monkeypatch.setattr(runner, "_post_github_comment", lambda r, i, b: bodies.append(b) or True)
    monkeypatch.setattr(runner, "_derive_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(runner, "_current_commit_sha", lambda: "abc1234")
    monkeypatch.setattr(runner, "_gate_cache_path", lambda sid: tmp_path / f"no-cache-{sid}.json")

    with pytest.raises(SystemExit):
        runner.post_verdict("syn-001-n", "pass", notes="Widget label looks correct to me")

    assert "Widget label looks correct to me" in bodies[0]


def test_post_verdict_invalid_verdict_exits_1(tmp_scenarios_dir, monkeypatch):
    """Unknown verdict string exits 1 without posting."""
    import claude_rts.qa_runner as runner

    _write_scenario_file(tmp_scenarios_dir, "syn-001-inv.py", "syn-001-inv")

    calls = []
    monkeypatch.setattr(runner, "_post_github_comment", lambda *a: calls.append(a) or True)

    with pytest.raises(SystemExit) as exc_info:
        runner.post_verdict("syn-001-inv", "maybe")

    assert exc_info.value.code == 1
    assert calls == []


def test_post_verdict_not_found_exits_1(tmp_scenarios_dir, monkeypatch):
    """Unknown scenario ID exits 1."""
    import claude_rts.qa_runner as runner

    with pytest.raises(SystemExit) as exc_info:
        runner.post_verdict("does-not-exist", "pass")

    assert exc_info.value.code == 1


def test_post_verdict_gh_failure_exits_1(tmp_scenarios_dir, tmp_path, monkeypatch):
    """If gh comment fails, exit 1."""
    import claude_rts.qa_runner as runner

    _write_scenario_file(tmp_scenarios_dir, "syn-001-ghf.py", "syn-001-ghf", debt_issue=221)

    monkeypatch.setattr(runner, "_post_github_comment", lambda *a: False)
    monkeypatch.setattr(runner, "_derive_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(runner, "_current_commit_sha", lambda: "abc1234")
    monkeypatch.setattr(runner, "_gate_cache_path", lambda sid: tmp_path / f"no-cache-{sid}.json")

    with pytest.raises(SystemExit) as exc_info:
        runner.post_verdict("syn-001-ghf", "pass")

    assert exc_info.value.code == 1


def test_post_verdict_includes_gate_question_when_cache_present(tmp_scenarios_dir, tmp_path, monkeypatch):
    """Gate question and expected appear in the comment when cache is available."""
    import claude_rts.qa_runner as runner
    from claude_rts.qa_scenario import HumanGate

    _write_scenario_file(tmp_scenarios_dir, "syn-001-gq.py", "syn-001-gq", debt_issue=221)

    bodies = []
    monkeypatch.setattr(runner, "_post_github_comment", lambda r, i, b: bodies.append(b) or True)
    monkeypatch.setattr(runner, "_derive_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(runner, "_current_commit_sha", lambda: "abc1234")

    gate = HumanGate(scenario_id="syn-001-gq", question="Is the label correct?", expected="Label reads X.")
    monkeypatch.setattr(runner, "_gate_cache_path", lambda sid: tmp_path / f"qa-gate-{sid}.json")
    runner._save_gate_cache("syn-001-gq", gate)

    with pytest.raises(SystemExit):
        runner.post_verdict("syn-001-gq", "pass")

    assert "Is the label correct?" in bodies[0]
    assert "Label reads X." in bodies[0]


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


def test_synthetic_scenario_run_setup_returns_human_gate():
    """run_setup should return a HumanGate when given a mock page."""
    import claude_rts.qa_runner as runner
    from claude_rts.qa_scenario import HumanGate

    path = pathlib.Path(__file__).resolve().parent.parent / "qa_scenarios" / "syn-001-container-terminal.py"
    cls = runner._load_scenario_class(path)
    assert cls is not None

    mock_page = mock.Mock()
    mock_page.evaluate.return_value = {"ok": True, "card_id": "card-abc"}

    instance = cls()
    gate = instance.run_setup(mock_page)

    assert isinstance(gate, HumanGate)
    assert gate.scenario_id == "syn-001-container-terminal"
    assert len(gate.question) > 10
    assert len(gate.expected) > 10
