# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Superseded pull-request CI runs do not leave a red required summary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from scripts.ci.ci_summary_gate import (
    EXIT_FAILURE,
    EXIT_PENDING,
    EXIT_SUCCESS,
    SELF_JOB_NAME,
    SKIPPABLE_GATE_JOBS,
    STRICT_GATE_JOBS,
    evaluate,
    has_newer_pull_request_run,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
NOW = datetime(2026, 9, 25, 17, 40, tzinfo=UTC)
PR_NUMBER = 2918
OLD_RUN_ID = 361_600_000_00
NEW_RUN_ID = OLD_RUN_ID + 17


def _z(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _job(
    name: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    completed_at: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": 1,
        "completed_at": completed_at or _z(NOW - timedelta(seconds=30)),
    }


def _healthy_jobs() -> list[dict[str, object]]:
    jobs = [_job(name) for name in (*STRICT_GATE_JOBS, *SKIPPABLE_GATE_JOBS)]
    jobs.append(_job("Tests (Split 1/1)"))
    jobs.append(_job(SELF_JOB_NAME, status="in_progress", conclusion=None))
    return jobs


def _with_cancelled_strict_job() -> list[dict[str, object]]:
    target = STRICT_GATE_JOBS[0]
    return [
        _job(target, conclusion="cancelled") if job["name"] == target else job
        for job in _healthy_jobs()
    ]


def _run(run_id: int, pr_number: int) -> dict[str, object]:
    return {
        "id": run_id,
        "event": "pull_request",
        "status": "in_progress",
        "pull_requests": [{"number": pr_number}],
    }


def test_newer_run_detection_is_scoped_to_the_same_pull_request() -> None:
    runs = [_run(NEW_RUN_ID, PR_NUMBER + 1), _run(OLD_RUN_ID - 1, PR_NUMBER)]
    assert not has_newer_pull_request_run(
        runs,
        current_run_id=OLD_RUN_ID,
        pr_number=PR_NUMBER,
    )

    runs.append(_run(NEW_RUN_ID, PR_NUMBER))
    assert has_newer_pull_request_run(
        runs,
        current_run_id=OLD_RUN_ID,
        pr_number=PR_NUMBER,
    )


def test_superseded_old_run_stays_pending_while_new_run_can_pass() -> None:
    """The old summary never posts red; the replacement run owns the verdict."""

    old_code, old_report = evaluate(
        _with_cancelled_strict_job(),
        now=NOW,
        superseded_by_newer_run=True,
    )
    assert old_code == EXIT_PENDING, old_report
    assert "own-job cancellations awaiting the newer run" in old_report

    new_code, new_report = evaluate(
        _healthy_jobs(),
        now=NOW,
        superseded_by_newer_run=False,
    )
    assert new_code == EXIT_SUCCESS, new_report


def test_cancelled_own_job_without_a_newer_run_fails_closed() -> None:
    code, report = evaluate(
        _with_cancelled_strict_job(),
        now=NOW,
        superseded_by_newer_run=False,
    )
    assert code == EXIT_FAILURE, report
    assert STRICT_GATE_JOBS[0] in report


def test_stale_or_unreadable_cancelled_own_job_fails_closed() -> None:
    target = STRICT_GATE_JOBS[0]
    for completed_at in (
        _z(NOW - timedelta(hours=1)),
        "not-a-timestamp",
        None,
    ):
        jobs = [
            (
                {
                    **job,
                    "conclusion": "cancelled",
                    "completed_at": completed_at,
                }
                if job["name"] == target
                else job
            )
            for job in _healthy_jobs()
        ]
        code, report = evaluate(
            jobs,
            now=NOW,
            superseded_by_newer_run=True,
        )
        assert code == EXIT_FAILURE, report


def test_ci_concurrency_cancels_pull_request_runs_only() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    concurrency = workflow["concurrency"]
    group = str(concurrency["group"])
    cancel = str(concurrency["cancel-in-progress"])

    assert "github.event.pull_request.number" in group
    assert "github.event_name == 'pull_request'" in group
    assert "github.ref" in group
    assert cancel == "${{ github.event_name == 'pull_request' }}"


def test_ci_summary_fetches_newer_runs_only_after_an_own_job_was_cancelled() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    summary = workflow["jobs"]["ci-summary"]
    step = next(
        item
        for item in summary["steps"]
        if item.get("name")
        == "Poll run jobs and compute fail-closed CI Summary verdict"
    )
    run = str(step["run"])

    assert "actions/workflows/ci.yml/runs?event=pull_request" in run
    assert 'select(.conclusion == "cancelled")' in run
    assert "--workflow-runs-file workflow_runs.json" in run
    assert step["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
