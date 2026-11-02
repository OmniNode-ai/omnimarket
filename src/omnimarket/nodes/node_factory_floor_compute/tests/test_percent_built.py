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
from tests.nodes.node_factory_floor_compute.support import pr, request

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
            prs=(f"repo#{n}",) if n < merged else (),
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
            prs=tuple(pr(f"repo#{n}", "MERGED") for n in range(DONE)),
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


def test_percent_built_reaches_100_with_every_work_task_merged_but_the_line_is_not_complete() -> (
    None
):
    plan = alignment_plan(PARTS - GATES)
    merged = tuple(pr(f"repo#{n}", "MERGED") for n in range(PARTS - GATES))
    line = compute_factory_floor(request(plans=(plan,), prs=merged)).lines[0]
    assert line.progress.percent == 100.0
    assert line.condition == "building"
