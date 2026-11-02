# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Built percent is the plan's parts done over all the parts of its own list (OMN-21930)."""

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
# them gates, 6 parts done (five adopted, one lane), and percent_built 12 (6 of the 48 parts that are not gates).
PARTS = 68
GATES = 20
DONE = 6


def alignment_plan() -> ModelFloorPlanInput:
    work = tuple(
        ModelFloorManifestTask(
            id=f"W{n}",
            title=f"W{n}",
            wave=str(n % GATES),
            prs=(f"repo#{n}",) if n < DONE else (),
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


def test_percent_built_is_merged_parts_over_every_part_of_the_plans_own_list() -> None:
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
    assert line.percent_built == 8  # 6 of 68 is 8.8, shown as 8, never 12 (6 of 48)


def test_percent_built_reaches_100_only_when_every_part_including_its_gates_is_done() -> (
    None
):
    plan = alignment_plan()
    merged = tuple(pr(f"repo#{n}", "MERGED") for n in range(PARTS - GATES))
    result = compute_factory_floor(request(plans=(plan,), prs=merged))
    assert result.lines[0].percent_built < 100
