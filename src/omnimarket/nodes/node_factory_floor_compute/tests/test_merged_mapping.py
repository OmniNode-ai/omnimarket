# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A task row is built from the merged pull requests of the tickets it names, not from a recorded status (OMN-21930)."""

from __future__ import annotations

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorManifest,
    ModelFloorManifestTask,
    ModelFloorMerge,
    ModelFloorPlanInput,
    ModelFloorPr,
    ModelFloorTableTask,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorItem,
)
from tests.nodes.node_factory_floor_compute.support import merge, pr, request

pytestmark = pytest.mark.unit

PLAN = "beta/plans/2026-10-28-validators-plan.md"


def validators_plan(*tasks: ModelFloorManifestTask) -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path=PLAN,
        title="Validators to core",
        tickets=("OMN-21433",),
        manifest=ModelFloorManifest(epic="OMN-21433", tasks=tasks),
    )


def items(
    plans: tuple[ModelFloorPlanInput, ...],
    *,
    prs: tuple[ModelFloorPr, ...] = (),
    merges: tuple[ModelFloorMerge, ...] = (),
) -> dict[str, ModelFloorItem]:
    result = compute_factory_floor(request(plans=plans, prs=prs, merges=merges))
    return {
        item.id: item for station in result.lines[0].stations for item in station.items
    }


def test_merged_mapping_reads_a_merge_the_pr_map_no_longer_keeps() -> None:
    # The watcher's merge list holds 2412 merges and its pull-request map 296 of them: a ticket whose pull request is
    # only in the merge list must still build its task.
    plan = validators_plan(
        ModelFloorManifestTask(id="T0.1", tickets=("OMN-21494",), scoped=True),
        ModelFloorManifestTask(id="T0.2", tickets=("OMN-21495",), scoped=True),
    )
    got = items(
        (plan,),
        merges=(
            merge(
                "omnibase_core#2110",
                60,
                title="T0.1: private vocabulary resolution as a node (OMN-21494)",
            ),
        ),
    )
    assert got["T0.1"].state == "done"
    assert got["T0.1"].prs == ("omnibase_core#2110",)
    assert got["T0.2"].state == "prep"


def test_merged_mapping_names_the_ticket_of_a_branch_when_the_title_names_none() -> (
    None
):
    plan = validators_plan(
        ModelFloorManifestTask(id="W0.1", tickets=("OMN-21635",), scoped=True)
    )
    got = items(
        (plan,),
        merges=(merge("plans_repo#1573", 60, title="docs: reuse verification"),),
        prs=(pr("plans_repo#1574", "MERGED", head_ref="omn-21635-w0-1-x"),),
    )
    assert got["W0.1"].prs == ("plans_repo#1574",)
    assert got["W0.1"].state == "done"


def test_merged_mapping_needs_every_named_ticket_merged_and_none_of_its_work_open() -> (
    None
):
    plan = validators_plan(
        ModelFloorManifestTask(
            id="T2.1", tickets=("OMN-21500", "OMN-21501"), scoped=True
        )
    )
    half = items(
        (plan,),
        merges=(merge("c#1", 60, title="T2.1: core hooks (OMN-21501)"),),
        prs=(pr("m#2", "OPEN", title="T2.1: wire six validators (OMN-21500)"),),
    )
    assert half["T2.1"].state == "active"
    both = items(
        (plan,),
        merges=(
            merge("c#1", 60, title="T2.1: core hooks (OMN-21501)"),
            merge("m#2", 30, title="T2.1: wire six validators (OMN-21500)"),
        ),
    )
    assert both["T2.1"].state == "done"
    one = items(
        (plan,),
        merges=(merge("c#1", 60, title="T2.1: core hooks (OMN-21501)"),),
    )
    assert one["T2.1"].state != "done"


def test_merged_mapping_never_shows_a_pr_on_a_row_it_is_not_the_work_of() -> None:
    # omnimarket#4814 is OMN-21957 (launch validity). A plan table listed it beside T1.1, whose ticket is OMN-21515.
    plan = validators_plan(
        ModelFloorManifestTask(
            id="T1.1",
            tickets=("OMN-21515",),
            prs=("omnimarket#4814", "omnibase_core#2124"),
            scoped=True,
        )
    )
    got = items(
        (plan,),
        merges=(
            merge(
                "omnimarket#4814",
                60,
                title="feat(OMN-21957): launch validity compute node, report-only first",
            ),
            merge(
                "omnibase_core#2124", 90, title="T1.1+K2: file shape node (OMN-21515)"
            ),
        ),
    )
    assert got["T1.1"].prs == ("omnibase_core#2124",)
    assert got["T1.1"].state == "done"
    alone = items(
        (plan.model_copy(),),
        merges=(
            merge("omnimarket#4814", 60, title="feat(OMN-21957): launch validity"),
        ),
    )
    assert "omnimarket#4814" not in alone["T1.1"].prs
    assert alone["T1.1"].state != "done"


def test_merged_mapping_takes_a_scoped_tasks_bare_id_only_from_its_own_plan() -> None:
    plan = validators_plan(ModelFloorManifestTask(id="S1", scoped=True))
    got = items(
        (plan,),
        merges=(
            merge("omnimarket#4796", 60, title="feat(OMN-21433): floor tokens (S1)"),
            merge("elsewhere#9", 30, title="feat(OMN-77): another plan's S1"),
        ),
    )
    assert got["S1"].prs == ("omnimarket#4796",)
    assert got["S1"].state == "done"


def test_merged_mapping_does_not_trust_a_table_rows_recorded_merged_state() -> None:
    plan = ModelFloorPlanInput(
        plan_path="beta/plans/2026-10-01-old-plan.md",
        title="Old plan",
        tickets=("OMN-7",),
        table_tasks=(
            ModelFloorTableTask(
                label="T1", text="first", tickets=("OMN-7",), state="merged"
            ),
        ),
    )
    recorded = items((plan,))
    assert recorded["T1"].state == "wait"
    backed = items((plan,), merges=(merge("r#1", 30, title="fix(OMN-7): first"),))
    assert backed["T1"].state == "done"
