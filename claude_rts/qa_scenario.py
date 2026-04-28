"""QA scenario contract — HumanGate dataclass and QAScenario protocol.

Scenario files placed in ``qa_scenarios/<id>.py`` must implement the
``QAScenario`` protocol.  The runner (``qa_runner.py``) imports each file,
instantiates the class, calls ``run_setup(page)`` with a live Playwright
``Page`` object, and then presents the returned ``HumanGate`` to the human.

Example minimal scenario
------------------------
```python
from claude_rts.qa_scenario import HumanGate, QAScenario

class Scenario(QAScenario):
    scenario_id = "pr220-s1-container-label"
    debt_issue = 221
    preset = "default"

    def run_setup(self, page) -> HumanGate:
        # Drive the app to the gate state using real UI interactions.
        page.wait_for_selector("#canvas")
        return HumanGate(
            scenario_id=self.scenario_id,
            question="Does the Container Manager widget title say 'Container Manager' (not VM Manager)?",
            expected="Title text reads 'Container Manager'",
        )
```
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class HumanGate:
    """Returned by a scenario's ``run_setup()`` to describe the human judgment gate.

    Attributes:
        scenario_id: Identifier matching the scenario class's ``scenario_id``.
        question:    Question printed to the terminal for the human to answer.
        expected:    Description of what a passing ("yes") state looks like.
                     Printed alongside the question for context.
    """

    scenario_id: str
    question: str
    expected: str


@runtime_checkable
class QAScenario(Protocol):
    """Protocol that every ``qa_scenarios/<id>.py`` file must satisfy.

    Class attributes (declare as class-level variables, not instance):

        scenario_id: str   — Unique identifier, e.g. "syn-001-container-terminal".
                             Used as the key in the verdicts JSONL and GitHub comments.
        debt_issue:  int   — GitHub issue number of the linked qa-debt issue (e.g. 221).
        preset:      str   — Dev-config preset name to start the server with.
                             Defaults to "default" if not declared.

    Instance method:

        run_setup(page: Page) -> HumanGate
            Drive the Playwright ``page`` to the visual state where human
            judgment is needed.  The page is already connected to the running
            app when this method is called.  Must use real app pathways — no
            ``/api/test/...`` endpoints.  Returns a ``HumanGate`` describing
            the question to present to the human.
    """

    scenario_id: str
    debt_issue: int
    preset: str

    def run_setup(self, page: Any) -> HumanGate:
        """Drive the app to the gate state. Return the HumanGate to present."""
        ...
