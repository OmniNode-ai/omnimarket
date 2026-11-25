# SPDX-License-Identifier: MIT
"""OMN-26424: the undeclared-read check is a STRICT CI Summary gate.

The job ``Undeclared Read Check (OMN-26424)`` was merged unconditionally in ci.yml
but absent from STRICT_GATE_JOBS, so a skipped or missing run did not refuse the
required ``CI Summary`` context. Known bad: the job skipped on a pull request
refuses. Known good: it passes when the job ran green. The fork case follows
``Coverage Sweep Gate``: the job's own ``if:`` skips it, it stays strict, and a
skip fails closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.ci import ci_summary_gate as gate
from scripts.ci.ci_summary_gate import (
    EXIT_FAILURE,
    EXIT_PENDING,
    EXIT_SUCCESS,
    SKIPPABLE_GATE_JOBS,
    STRICT_GATE_JOBS,
    evaluate,
)
from tests.unit.scripts.ci.test_ci_summary_gate import _healthy_jobs, _job

REPO_ROOT = Path(__file__).resolve().parents[4]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JOB_KEY = "undeclared-read-check"
DISPLAY_NAME = "Undeclared Read Check (OMN-26424)"

pytestmark = pytest.mark.unit


def _with(
    conclusion: str | None, *, status: str = "completed"
) -> list[dict[str, object]]:
    jobs = [j for j in _healthy_jobs() if j["name"] != DISPLAY_NAME]
    jobs.append(_job(DISPLAY_NAME, status=status, conclusion=conclusion))
    return jobs


def test_display_name_matches_the_ci_job() -> None:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    assert jobs[JOB_KEY]["name"] == DISPLAY_NAME


def test_job_is_strict_not_skippable() -> None:
    assert DISPLAY_NAME in STRICT_GATE_JOBS
    assert DISPLAY_NAME not in SKIPPABLE_GATE_JOBS


def test_known_good_job_ran_green_passes() -> None:
    code, report = evaluate(_with("success"), event="pull_request")
    assert code == EXIT_SUCCESS, report


@pytest.mark.parametrize("conclusion", ["skipped", "cancelled", "failure"])
def test_known_bad_job_not_green_refuses(conclusion: str) -> None:
    code, report = evaluate(_with(conclusion), event="pull_request")
    assert code == EXIT_FAILURE, report
    assert DISPLAY_NAME in report


def test_known_bad_job_absent_never_passes() -> None:
    """Absent holds at PENDING; the caller's deadline turns PENDING into a failure."""
    jobs = [j for j in _healthy_jobs() if j["name"] != DISPLAY_NAME]
    code, report = evaluate(jobs, event="pull_request")
    assert code == EXIT_PENDING, report
    assert DISPLAY_NAME in report


def test_gate_module_loads_the_strict_row() -> None:
    assert DISPLAY_NAME in gate.GATE_JOBS
