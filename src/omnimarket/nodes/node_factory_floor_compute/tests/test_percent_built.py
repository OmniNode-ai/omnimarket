# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Built percent is merged work tasks over the plan's work tasks; its gates are counted apart."""

from __future__ import annotations

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorManifest,
    ModelFloorManifestTask,
    ModelFloorPlanInput,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from tests.nodes.node_factory_floor_compute.support import (
    pr,
    proven,
    request,
    running_on_dev,
)

pytestmark = pytest.mark.unit

ALIGNMENT = "beta/plans/2026-10-31-eighth-wave-plan.md"
# The record the live floor published at 2026-11-02T01:34:13Z for this plan: 68 parts in the plan's own list, 20 of
# them gates, 6 parts done (five adopted, one lane), and a built percent of 6 of the 48 work tasks that are not gates.
PARTS = 68
GATES = 20
DONE = 6


def alignment_plan(merged: int = DONE) -> ModelFloorPlanInput:
    work = tuple(
        ModelFloorManifestTask(
            id=f"W{n}",
            title=f"W{n}",
            wave=str(n % GATES),
            prs=(f"repo#{n + 1}",) if n < merged else (),
        )
        for n in range(PARTS - GATES)
    )
    gates = tuple(
        ModelFloorManifestTask(id=f"G{n}", title=f"G{n}", wave=str(n), kind="gate")
        for n in range(GATES)
    )
    return ModelFloorPlanInput(
        plan_path=ALIGNMENT,
        title="Orchestration alignment",
        manifest=ModelFloorManifest(tasks=work + gates),
    )


def test_percent_built_is_merged_work_tasks_over_the_work_tasks_only() -> None:
    result = compute_factory_floor(
        request(
            plans=(alignment_plan(),),
            prs=tuple(pr(f"repo#{n + 1}", "MERGED") for n in range(DONE)),
            ledger=proven(*(f"repo#{n + 1}" for n in range(DONE))),
            running_evidence=running_on_dev(*(f"repo#{n + 1}" for n in range(DONE))),
        )
    )
    line = result.lines[0]
    parts = [item for station in line.stations for item in station.items]
    assert len(parts) == PARTS
    assert sum(item.state == "done" for item in parts) == DONE
    # 6 of 48 work tasks is 12.5, never 6 of 68 and never truncated to 12; the 20 gates stand apart.
    assert (line.progress.completed, line.progress.total) == (DONE, PARTS - GATES)
    assert line.progress.percent == 12.5
    assert (line.gates.passed, line.gates.failed, line.gates.waiting) == (0, 0, GATES)


def test_percent_built_does_not_count_merged_parts_that_are_not_running_on_dev() -> (
    None
):
    result = compute_factory_floor(
        request(
            plans=(alignment_plan(),),
            prs=tuple(pr(f"repo#{n + 1}", "MERGED") for n in range(DONE)),
            running_evidence=(),
        )
    )
    line = result.lines[0]
    parts = [item for station in line.stations for item in station.items]
    assert sum(item.state == "integrating" for item in parts) == DONE
    assert line.progress.completed == 0
    assert line.progress.percent == 0.0
    assert line.line_evidence.merged == DONE
    assert line.line_evidence.running_on_dev == 0


def test_percent_built_reaches_100_with_every_work_task_done_but_the_line_is_not_complete() -> (
    None
):
    plan = alignment_plan(PARTS - GATES)
    keys = tuple(f"repo#{n + 1}" for n in range(PARTS - GATES))
    line = compute_factory_floor(
        request(
            plans=(plan,),
            prs=tuple(pr(key, "MERGED") for key in keys),
            ledger=proven(*keys),
            running_evidence=running_on_dev(*keys),
        )
    ).lines[0]
    assert line.progress.percent == 100.0
    assert line.condition == "building"
