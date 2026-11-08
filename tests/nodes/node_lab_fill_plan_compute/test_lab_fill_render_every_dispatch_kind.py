# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every kind the dispatch plan can send renders a lane brief with a task (OMN-22986).

``DISPATCH_KINDS`` gained the tier-1 source kinds (plan-stalled, ruling-pending,
lane-resend), but the render table had no entry for them, so their brief read ``TASK: ``
with nothing after it and the dispatched lane started with no work named.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_dispatch_plan import (
    DISPATCH_KINDS,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_lane_render import (
    HandlerLabFillLaneRender,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillDispatchItem,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models.model_lab_fill_lane_render import (
    ModelLabFillLaneRenderRequest,
    ModelLabFillRenderConfig,
)

pytestmark = pytest.mark.unit


def _brief(kind: str) -> str:
    item = ModelLabFillDispatchItem(
        lane=f"lab-fill-{kind}",
        kind=kind,
        id="row-1",
        ticket="OMN-9",
        pr="",
        repo="repo_a",
        ref="main",
        title="the ticket title",
        updated_at="2026-11-07T00:00:00Z",
        host="h1",
        pinned=False,
        engine="sonnet",
        route="ROUTE: band=B3 score=10",
    )
    config = ModelLabFillRenderConfig(
        pillar="landing",
        parent_lane="lab-fill",
        parent_ticket="OMN-22986",
        run_key="2026-11-07",
        authority_ruling="2026-10-10T04:08:23Z",
        authority_lane="ruling-idle-lab-9f8a",
    )
    return (
        HandlerLabFillLaneRender()
        .handle(ModelLabFillLaneRenderRequest(item=item, config=config))
        .brief
    )


@pytest.mark.parametrize("kind", DISPATCH_KINDS)
def test_every_dispatch_kind_names_a_task_in_its_brief(kind: str) -> None:
    task = next(line for line in _brief(kind).splitlines() if line.startswith("TASK:"))
    assert task.removeprefix("TASK:").strip(), f"{kind} renders an empty TASK line"
