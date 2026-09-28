# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""CI Summary holds a red external context while its producer runs again (OMN-17427).

A port of omnibase_infra#4225. There, CI Summary failed a Hostile Reviewer
context at the fixed re-run window while the replacement run of the same
workflow, for the same head, was queued for a runner; a rerun with no change to
the head passed. ``replacement_run_in_flight`` reads the head's
``actions/runs?head_sha=`` payload and holds such a row PENDING for as long as
that execution runs. It never greens a row.

This is prevention: no omnimarket instance was measured on 2026-09-27. The
replay below is omnimarket#2913 CI run 36349798545 attempt 1, the one
candidate, and it is a negative control: nothing was in flight at 21:17:34Z,
so the red stays a failure.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from scripts.ci.ci_summary_gate import (
    EXIT_FAILURE,
    EXIT_PENDING,
    CheckRunState,
    check_run_workflow_run_id,
    evaluate_external,
    replacement_run_in_flight,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests/ci/fixtures/omn17427_in_flight/mkt2913_at_211734.json"
CI_YML = REPO_ROOT / ".github/workflows/ci.yml"
HOSTILE_WF = 259545846
RED_ROW_RUN = 36349797572
# ACTOR_CONDITIONAL_CONTEXTS is empty, so the PR author does not change the verdict.
ACTOR = "pr-author"


def _state(
    run_id: int | None, completed_at: str = "2026-09-27T20:57:15Z"
) -> CheckRunState:
    return CheckRunState(
        name="Hostile Review Gate",
        status="completed",
        conclusion="failure",
        started_at="2026-09-27T20:55:30Z",
        id=1,
        completed_at=completed_at,
        workflow_run_id=run_id,
    )


def _run(run_id: int, status: str, **kw: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": run_id,
        "workflow_id": HOSTILE_WF,
        "event": "pull_request",
        "status": status,
        "run_started_at": "2026-09-27T20:55:20Z",
    }
    row.update(kw)
    return row


def test_the_run_id_is_read_from_the_row_url() -> None:
    assert (
        check_run_workflow_run_id(
            {"html_url": "https://github.com/o/r/actions/runs/42/job/7"}
        )
        == 42
    )
    assert (
        check_run_workflow_run_id({"html_url": "https://github.com/o/r/runs/7"}) is None
    )


def test_a_newer_run_of_the_same_workflow_and_event_holds() -> None:
    runs = [_run(RED_ROW_RUN, "completed"), _run(RED_ROW_RUN + 5, "queued")]
    assert replacement_run_in_flight(_state(RED_ROW_RUN), runs)


def test_only_the_same_workflow_and_event_and_an_unfinished_run_count() -> None:
    own = _run(RED_ROW_RUN, "completed")
    assert not replacement_run_in_flight(
        _state(RED_ROW_RUN), [own, _run(RED_ROW_RUN + 5, "queued", workflow_id=1)]
    )
    assert not replacement_run_in_flight(
        _state(RED_ROW_RUN), [own, _run(RED_ROW_RUN + 5, "queued", event="push")]
    )
    assert not replacement_run_in_flight(
        _state(RED_ROW_RUN), [own, _run(RED_ROW_RUN + 5, "completed")]
    )
    assert not replacement_run_in_flight(
        _state(RED_ROW_RUN), [own, _run(RED_ROW_RUN - 5, "queued")]
    )


def test_a_rerun_attempt_started_after_the_row_concluded_holds() -> None:
    rerun = _run(RED_ROW_RUN, "in_progress", run_started_at="2026-09-27T21:37:52Z")
    assert replacement_run_in_flight(_state(RED_ROW_RUN), [rerun])
    stale = _run(RED_ROW_RUN, "in_progress", run_started_at="2026-09-27T20:55:20Z")
    assert not replacement_run_in_flight(_state(RED_ROW_RUN), [stale])


def test_fail_closed_without_a_url_a_run_or_a_payload() -> None:
    runs = [_run(RED_ROW_RUN, "completed"), _run(RED_ROW_RUN + 5, "queued")]
    assert not replacement_run_in_flight(_state(None), runs)
    assert not replacement_run_in_flight(_state(RED_ROW_RUN), None)
    assert not replacement_run_in_flight(_state(RED_ROW_RUN), [])
    assert not replacement_run_in_flight(_state(999), runs)


def _snapshot() -> tuple[list[dict[str, object]], list[dict[str, object]], datetime]:
    snap = json.loads(FIXTURE.read_text(encoding="utf-8"))
    now = datetime.fromisoformat(snap["at"].replace("Z", "+00:00"))
    return snap["check_runs"], snap["workflow_runs"], now


def test_replay_of_omnimarket_2913_is_a_negative_control() -> None:
    """CI run 36349798545 attempt 1 logged ``external-context failures: Hostile
    Review Gate, Hostile Reviewer (adversarial gate)`` at 21:17:34Z. Reconstructed
    from live check-runs with CI's later attempt replaced by attempt 1's jobs.
    Hostile Reviewer run 36349797572 attempt 1 had failed at 20:57:15Z and
    attempt 2 started at 21:37:52Z, so no replacement was in flight: the same
    failure before and after this change."""
    check_runs, runs, now = _snapshot()
    expected = (
        "external-context failures: Hostile Review Gate, "
        "Hostile Reviewer (adversarial gate)"
    )
    old_code, old_report = evaluate_external(check_runs, actor=ACTOR, now=now)
    new_code, new_report = evaluate_external(
        check_runs, actor=ACTOR, now=now, head_workflow_runs=runs
    )
    assert old_code == new_code == EXIT_FAILURE
    assert expected in old_report
    assert expected in new_report


def test_the_same_head_with_a_queued_replacement_is_pending() -> None:
    """The omnibase_infra#4216 shape laid on the omnimarket#2913 snapshot: a
    newer Hostile Reviewer run for the same head is queued. Synthetic: the
    queued run is added to the captured payload."""
    check_runs, runs, now = _snapshot()
    base = next(r for r in runs if r["id"] == RED_ROW_RUN)
    queued = copy.deepcopy(base)
    queued.update(id=RED_ROW_RUN + 1000, status="queued", conclusion=None)
    code, report = evaluate_external(
        check_runs, actor=ACTOR, now=now, head_workflow_runs=[*runs, queued]
    )
    assert code == EXIT_PENDING, report
    assert "running again on this head" in report
    assert "external-context failures" not in report


def test_ci_summary_passes_the_head_workflow_runs() -> None:
    doc = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    runs = "\n".join(
        str(step.get("run") or "")
        for job in doc["jobs"].values()
        for step in job.get("steps", [])
    )
    assert "--head-workflow-runs-file head_workflow_runs.json" in runs
    assert "actions/runs?head_sha=${HEAD_SHA}" in runs
