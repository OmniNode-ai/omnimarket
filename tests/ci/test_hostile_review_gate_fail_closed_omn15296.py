# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15296: the Hostile Review Gate must fail closed on every non-success result.

``needs.<job>.result`` takes one of four values -- ``success``, ``failure``,
``cancelled`` or ``skipped``. ``hostile-review-gate`` renders the required status
check ``Hostile Review Gate`` on ``dev``, so any value it lets through is a
merge-eligible state. A cancelled or skipped reviewer produced no verdict and is
therefore not evidence that the review passed.

Live defect this locks: omnimarket#1920, run 30298837182 -- the reviewer job was
cancelled at 04:13:14Z and the gate reported SUCCESS at 04:13:21Z.

These tests execute the workflow's own ``run:`` script -- the text that actually
runs in CI -- with the GitHub expression substituted, rather than asserting
against a reimplementation of the logic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "hostile-reviewer.yml"

GATE_JOB_ID = "hostile-review-gate"
REVIEWER_JOB_ID = "hostile-review"
RESULT_EXPRESSION = "${{ needs." + REVIEWER_JOB_ID + ".result }}"

# Every value GitHub assigns to needs.<job>.result other than `success`, plus a
# value GitHub does not define today -- a result this gate cannot interpret must
# fail closed rather than fall through to the pass path.
NON_SUCCESS_RESULTS = ("failure", "cancelled", "skipped", "some_future_github_value")


def _gate_job() -> dict[str, Any]:
    parsed = cast(dict[str, Any], yaml.safe_load(WORKFLOW_PATH.read_text()))
    jobs = cast(dict[str, Any], parsed["jobs"])
    assert GATE_JOB_ID in jobs, f"job '{GATE_JOB_ID}' not found; jobs: {list(jobs)}"
    return cast(dict[str, Any], jobs[GATE_JOB_ID])


def _gate_script() -> str:
    """Return the single ``run:`` script the gate job executes."""
    scripts = [str(step["run"]) for step in _gate_job()["steps"] if "run" in step]
    assert len(scripts) == 1, (
        f"expected exactly one run-step in '{GATE_JOB_ID}', found {len(scripts)}. "
        "The substitution below assumes one decision point; update this test "
        "before splitting the gate across steps."
    )
    return scripts[0]


def _run_gate(result: str) -> subprocess.CompletedProcess[str]:
    """Execute the real gate script with the reviewer result substituted in."""
    script = _gate_script().replace(RESULT_EXPRESSION, result)
    assert "${{" not in script, (
        "an unsubstituted GitHub expression remains in the gate script; this test "
        f"would be evaluating shell that never runs as written. Script:\n{script}"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.unit
class TestHostileReviewGateFailsClosed:
    """The gate opens only on a real reviewer verdict."""

    def test_workflow_exists(self) -> None:
        assert WORKFLOW_PATH.exists(), f"workflow not found: {WORKFLOW_PATH}"

    def test_gate_consumes_the_reviewer_result(self) -> None:
        """The gate must read the reviewer job's result, or it gates nothing."""
        assert REVIEWER_JOB_ID in _gate_job().get("needs", []), (
            f"'{GATE_JOB_ID}' must declare needs: [{REVIEWER_JOB_ID}]"
        )
        assert RESULT_EXPRESSION in _gate_script(), (
            f"gate script must evaluate {RESULT_EXPRESSION}"
        )

    def test_gate_runs_even_when_the_reviewer_does_not(self) -> None:
        """`if: always()` is what makes the non-success paths reachable at all.

        Without it the gate job itself goes `skipped` when the reviewer dies,
        and branch protection has historically read a skipped required context
        as passing -- the same vacuous green by a different route.
        """
        assert str(_gate_job().get("if", "")).strip() == "always()", (
            f"'{GATE_JOB_ID}' must set `if: always()`"
        )

    def test_gate_passes_on_success(self) -> None:
        """A completed reviewer run with no critical findings still merges."""
        completed = _run_gate("success")
        assert completed.returncode == 0, (
            "gate must pass on a real success verdict; "
            f"got exit {completed.returncode}\nstdout:\n{completed.stdout}"
        )

    @pytest.mark.parametrize("result", NON_SUCCESS_RESULTS)
    def test_gate_blocks_on_every_non_success_result(self, result: str) -> None:
        """`cancelled`, `skipped` and unknown values must not open the gate.

        This is the OMN-15296 regression: the gate previously tested only
        `= "failure"`, so a cancelled reviewer fell through to
        "Hostile Review Gate PASSED" and the required context went green with
        no adversarial verdict in existence.
        """
        completed = _run_gate(result)
        assert completed.returncode != 0, (
            f"gate must FAIL CLOSED on result '{result}', but it exited 0.\n"
            f"stdout:\n{completed.stdout}"
        )
        assert "::error::" in completed.stdout, (
            f"blocking on '{result}' must emit an ::error:: annotation naming the "
            f"reason; stdout:\n{completed.stdout}"
        )

    def test_pass_path_is_not_reachable_without_success(self) -> None:
        """The literal pass message must never print for a non-success result."""
        for result in NON_SUCCESS_RESULTS:
            completed = _run_gate(result)
            assert "Hostile Review Gate PASSED" not in completed.stdout, (
                f"gate printed its PASSED message for result '{result}'"
            )
