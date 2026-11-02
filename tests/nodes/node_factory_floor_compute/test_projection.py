# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection every surface reads: condition, blocker, progress, gates, line evidence and data age."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorLinearIssue,
    ModelFloorManifest,
    ModelFloorManifestTask,
    ModelFloorPlanInput,
    ModelFloorSourceReads,
)
from omnimarket.models.model_merged_running_count import ModelMrcEvidence
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorLine,
)
from tests.nodes.node_factory_floor_compute.support import (
    NOW,
    ago,
    merge,
    pr,
    request,
    row,
)

pytestmark = pytest.mark.unit

PLAN = "beta/plans/2026-11-02-projection-plan.md"
EPIC = "OMN-60"


def work(
    task_id: str, *tickets: str, prs: tuple[str, ...] = ()
) -> ModelFloorManifestTask:
    return ModelFloorManifestTask(
        id=task_id, title=f"{task_id} work", tickets=tickets, prs=prs, scoped=True
    )


def gate(task_id: str, *depends_on: str) -> ModelFloorManifestTask:
    return ModelFloorManifestTask(
        id=task_id, title=task_id, kind="gate", depends_on=depends_on, scoped=True
    )


def plan_of(
    *tasks: ModelFloorManifestTask, scope_status: Any = "valid", scope_note: str = ""
) -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path=PLAN,
        title="Projection plan",
        tickets=(EPIC,),
        manifest=ModelFloorManifest(
            tasks=tasks, scope_status=scope_status, scope_note=scope_note
        ),
    )


def gate_row(gate_id: str, verdict: str) -> Any:
    return row(
        "STATUS", 5, "orchestrator", state="gate", subject=gate_id, verdict=verdict
    )


def line_of(*args: Any, **kwargs: Any) -> ModelFloorLine:
    return compute_factory_floor(request(*args, **kwargs)).lines[0]


def values(node: Any) -> list[Any]:
    """Every leaf value of a dumped record."""
    if isinstance(node, dict):
        return [leaf for child in node.values() for leaf in values(child)]
    if isinstance(node, (list, tuple)):
        return [leaf for child in node for leaf in values(child)]
    return [node]


def test_line_b_work_tasks_and_gates() -> None:
    # The live Line B: 68 parts, 20 of them named gates, 6 work tasks merged.
    tasks = tuple(
        work(f"W{n}", prs=(f"repo#{n}",) if n < 6 else ()) for n in range(48)
    ) + tuple(gate(f"G{n}") for n in range(20))
    ledger = (gate_row("G0", "pass"), gate_row("G1", "fail"))
    line = line_of(
        plans=(plan_of(*tasks),),
        prs=tuple(pr(f"repo#{n}", "MERGED") for n in range(6)),
        ledger=ledger,
    )
    assert line.progress.state == "known"
    assert (line.progress.completed, line.progress.total) == (6, 48)
    assert line.progress.percent == 12.5
    assert line.progress.note == "6 / 48 work tasks complete"
    assert (line.gates.passed, line.gates.failed, line.gates.waiting) == (1, 1, 18)
    assert len(line.gates.items) == 20
    assert {ref.id: ref.state for ref in line.gates.items}["G1"] == "failed"
    assert (line.line_evidence.merged, line.line_evidence.of) == (6, 48)


def test_percent_rounds_half_up_to_one_decimal() -> None:
    tasks = tuple(work(f"W{n}", prs=(f"repo#{n}",) if n < 1 else ()) for n in range(8))
    line = line_of(plans=(plan_of(*tasks),), prs=(pr("repo#0", "MERGED"),))
    assert line.progress.percent == 12.5
    tasks = tuple(work(f"W{n}", prs=(f"repo#{n}",) if n < 1 else ()) for n in range(3))
    line = line_of(plans=(plan_of(*tasks),), prs=(pr("repo#0", "MERGED"),))
    assert line.progress.percent == 33.3


def test_one_condition_building_keeps_blocker() -> None:
    # Line C: a ledger stop on T3 while a live lane named for T10 works the line.
    stop = row(
        "TERMINAL",
        30,
        "ended-lane",
        row_id="stop-1",
        tickets=("OMN-3",),
        outcome="blocked",
        reason="the gate refused",
    )
    line = line_of(
        plans=(plan_of(work("T3", "OMN-3"), work("T10")),),
        ledger=(
            stop,
            row("CLAIM", 5, "one-rt-t10-delegation-1101", tickets=(EPIC,)),
        ),
    )
    assert line.condition == "building"
    assert line.blocker is not None
    assert "the gate refused" in line.blocker.reason
    assert "stop-1" in line.blocker.evidence
    assert "stopped" not in values(line.model_dump(mode="json"))


def test_a_soft_stop_without_a_live_lane_is_stopped_with_the_same_blocker() -> None:
    stop = row(
        "TERMINAL", 30, "ended-lane", tickets=("OMN-3",), outcome="blocked", reason="x"
    )
    line = line_of(plans=(plan_of(work("T3", "OMN-3"), work("T10")),), ledger=(stop,))
    assert line.condition == "stopped"
    assert line.blocker is not None


def test_unknown_is_not_zero() -> None:
    # No manifest and no table: the total is missing.
    missing = line_of(plans=(ModelFloorPlanInput(plan_path=PLAN),))
    assert missing.progress.state == "unknown"
    assert (missing.progress.completed, missing.progress.total) == (None, None)
    assert missing.progress.percent is None
    # A manifest declaring no work task.
    none_declared = line_of(plans=(plan_of(),))
    assert none_declared.progress.state == "no_tasks"
    assert none_declared.progress.note == "No work tasks declared"
    assert none_declared.progress.percent is None
    # Only gates declared is still no work tasks.
    assert line_of(plans=(plan_of(gate("G0")),)).progress.state == "no_tasks"
    # A real 0: a known valid total with nothing complete.
    zero = line_of(plans=(plan_of(*(work(f"W{n}") for n in range(6))),))
    assert zero.progress.state == "known"
    assert (zero.progress.completed, zero.progress.total) == (0, 6)
    assert zero.progress.percent == 0.0


def test_scope_under_review_is_unknown_progress_and_needs_review() -> None:
    plan = plan_of(
        work("R1"),
        work("R2"),
        scope_status="needs_review",
        scope_note="R8 is not a task",
    )
    line = line_of(plans=(plan,), prs=())
    assert line.progress.state == "unknown"
    assert line.progress.percent is None
    assert "R8 is not a task" in line.progress.note
    assert line.condition == "needs_review"
    assert len(line.stations) == 2  # no task is silently removed


def test_review_scope_keeps_a_soft_blocker_without_stopping_the_line() -> None:
    stop = row(
        "TERMINAL", 30, "ended-lane", tickets=("OMN-3",), outcome="blocked", reason="R8"
    )
    plan = plan_of(work("T3", "OMN-3"), scope_status="needs_review")
    line = line_of(plans=(plan,), ledger=(stop,))
    assert line.condition == "needs_review"
    assert line.blocker is not None


def test_duplicate_task_ids_make_the_total_unknown() -> None:
    line = line_of(plans=(plan_of(work("A"), work("A")),))
    assert line.progress.state == "unknown"
    assert line.progress.total is None


def test_all_tasks_done_gate_pending_not_complete() -> None:
    tasks = (
        work("A", prs=("repo#1",)),
        work("B", prs=("repo#2",)),
        gate("G0", "A", "B"),
    )
    prs = (pr("repo#1", "MERGED"), pr("repo#2", "MERGED"))
    waiting = line_of(plans=(plan_of(*tasks),), prs=prs)
    assert waiting.progress.percent == 100.0
    assert waiting.condition == "building"
    assert waiting.blocker is not None
    assert "G0" in waiting.blocker.reason
    assert waiting.blocker.evidence == ("G0",)
    failed = line_of(
        plans=(plan_of(*tasks),), prs=prs, ledger=(gate_row("G0", "fail"),)
    )
    assert failed.progress.percent == 100.0
    assert failed.condition == "stopped"
    passed = line_of(
        plans=(plan_of(*tasks),), prs=prs, ledger=(gate_row("G0", "pass"),)
    )
    assert passed.condition == "complete"
    assert passed.blocker is None


def test_merged_only_percent_and_separate_evidence() -> None:
    tasks = (
        work("A", "OMN-1", prs=("repo#1",)),
        work("B", "OMN-2"),
        work("C", "OMN-3"),
    )
    ledger = (row("TERMINAL", 10, "lane-b", tickets=("OMN-2",), outcome="done"),)
    linear = (ModelFloorLinearIssue(id="OMN-3", status_type="completed"),)
    line = line_of(
        plans=(plan_of(*tasks),),
        prs=(pr("repo#1", "MERGED"),),
        merges=(merge("repo#1", 90),),
        ledger=ledger,
        linear=linear,
    )
    items = {item.id: item for station in line.stations for item in station.items}
    assert [items[k].state for k in "ABC"] == ["done", "done", "done"]
    assert [items[k].evidence_kind for k in "ABC"] == [
        "merged",
        "terminal",
        "linear_done",
    ]
    # Terminal and closed-ticket work counts toward the condition, never toward the progress.
    assert (line.progress.completed, line.progress.total) == (1, 3)
    assert line.condition == "complete"
    # Neither source of running-on-dev was read: None, never 0.
    assert line.line_evidence.merged == 1
    assert line.line_evidence.running_on_dev is None
    assert line.line_evidence.verified is None
    assert line.line_evidence.of == 3


def test_merged_only_percent_and_separate_evidence_when_running_is_read() -> None:
    tasks = (work("A", prs=("repo#1",)), work("B", prs=("repo#2",)), work("C"))
    running = (
        ModelMrcEvidence(
            repo="OmniNode-ai/repo",
            kind="deploy",
            source="x",
            number=1,
            observed_at=ago(30),
        ),
    )
    proof = row(
        "STATUS", 20, "prover", state="integration", verdict="pass", prs=("repo#1",)
    )
    line = line_of(
        plans=(plan_of(*tasks),),
        prs=(pr("repo#1", "MERGED"), pr("repo#2", "MERGED")),
        merges=(merge("repo#1", 90), merge("repo#2", 80)),
        ledger=(proof,),
        running_evidence=running,
    )
    assert line.line_evidence.merged == 2
    assert line.line_evidence.running_on_dev == 1
    assert line.line_evidence.verified == 1
    assert line.line_evidence.of == 3
    unread = line_of(
        plans=(plan_of(*tasks),),
        prs=(pr("repo#1", "MERGED"),),
        merges=(merge("repo#1", 90),),
        ledger=(proof,),
    )
    assert (unread.line_evidence.running_on_dev, unread.line_evidence.verified) == (
        None,
        None,
    )


def test_data_age_on_every_line() -> None:
    reads = ModelFloorSourceReads(ledger=NOW - timedelta(minutes=50))
    result = compute_factory_floor(
        request(
            plans=(plan_of(work("A")),),
            selection=("projection-plan", "no-such-plan"),
            source_reads=reads,
        )
    )
    assert [line.source for line in result.lines] == ["manifest", "undrawn"]
    for line in result.lines:
        # The oldest source read is the ledger's, 50 minutes before the record time (the watcher ticked 3 minutes before).
        assert line.data_age.read_at == "2026-10-31T11:10:00Z"
        assert line.data_age.age_min == 50


def test_data_age_without_other_read_times_is_the_watcher_tick() -> None:
    line = line_of(plans=(plan_of(work("A")),))
    assert (line.data_age.read_at, line.data_age.age_min) == ("2026-10-31T11:57:00Z", 3)


def test_undrawn_line_projects_unknown_progress_and_a_blocker() -> None:
    result = compute_factory_floor(
        request(plans=(plan_of(work("A")),), selection=("nope",))
    )
    undrawn = next(line for line in result.lines if line.source == "undrawn")
    assert undrawn.condition == "stopped"
    assert undrawn.progress.state == "unknown"
    assert undrawn.blocker is not None
    assert undrawn.blocker.evidence == ("nope",)
    assert undrawn.gates.items == ()
    assert undrawn.line_evidence.running_on_dev is None
