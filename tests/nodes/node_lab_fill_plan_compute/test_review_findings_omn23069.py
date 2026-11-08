# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review finding omnimarket#5305:2 (OMN-23069): machinery ordering must be transitive.

The comparator judged a pair that holds a machinery lane by the contract's tier order alone and every
other pair by priority class first. Under a supported tier policy that moves the ticket group ahead of
machinery, ticket < machinery (tier), machinery < process-fix (tier) and process-fix < ticket (class)
made a cycle, so the sorted order, and the lane that won a scarce slot, depended on enumeration order.
"""

from __future__ import annotations

import itertools

import pytest

from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_dispatch_plan import (
    HandlerLabFillDispatchPlan,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.helpers_machinery_policy import (
    machinery_policy,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillCapacityResult,
    ModelLabFillDispatchPlanRequest,
    ModelLabFillHostCapacity,
    ModelLabFillMachineryPolicy,
    ModelLabFillPlanConfig,
)

pytestmark = pytest.mark.unit

PROCESS_FIX = {
    "kind": "process-fix",
    "ticket": "OMN-300",
    "id": "fix-300",
    "repo": "omnimarket",
    "title": "process fix",
}
TICKET = {
    "kind": "ticket",
    "ticket": "OMN-100",
    "title": "a ticket",
    "under_parent": True,
}
MACHINERY = {
    "kind": "machinery",
    "ticket": "",
    "title": "factory break",
    "machinery_key": "friction:lab-idle",
    "priority_class": "mvp-landing",
    "evidence": "5 FRICTION rows",
}
PR_RED = {
    "kind": "pr-red",
    "ticket": "OMN-200",
    "title": "red pr",
    "pr": "omnimarket#9",
    "repo": "omnimarket",
    "under_parent": True,
}
CANDIDATES = (PROCESS_FIX, TICKET, MACHINERY, PR_RED)


def _policy(tier_order: list[list[str]]) -> ModelLabFillMachineryPolicy:
    base = machinery_policy().model_dump()
    return ModelLabFillMachineryPolicy.model_validate(
        {**base, "tier_order": tier_order}
    )


def _dispatch(
    candidates: tuple[dict[str, object], ...],
    policy: ModelLabFillMachineryPolicy,
    max_lanes: int = 12,
) -> list[str]:
    host = ModelLabFillHostCapacity(
        name="h051",
        lanes=12,
        idle_lanes=12,
        runner_slots=12,
        cap_bound=False,
        codex=True,
        claude=True,
        running=0,
        load_per_core=0.1,
        reason="",
    )
    plan = HandlerLabFillDispatchPlan().handle(
        ModelLabFillDispatchPlanRequest(
            candidates=candidates,
            capacity=ModelLabFillCapacityResult(hosts=(host,), free=12, budget=12),
            config=ModelLabFillPlanConfig(
                parent_ticket="OMN-17427",
                milestone="m",
                run_key="2026-11-03T2037Z",
                max_lanes=max_lanes,
                mvp_repos=("omnimarket",),
            ),
            machinery=policy,
        )
    )
    return [i.kind for i in plan.dispatch]


# The ticket group moved ahead of machinery, as test_lab_fill_machinery_tier_contract_data supports.
TICKET_FIRST = [
    ["ticket", "plan-stalled", "ruling-pending"],
    ["machinery"],
    ["lane-resend"],
    ["pr-red"],
    ["pr-stalled"],
    ["defect"],
    ["process-fix", "partial-node", "wiring"],
    ["pr-land"],
    ["dod-verify"],
]
# Machinery sharing a rank with the ticket group.
TICKET_SHARES_MACHINERY = [
    ["machinery", "ticket", "plan-stalled", "ruling-pending"],
    ["lane-resend"],
    ["pr-red"],
    ["pr-stalled"],
    ["defect"],
    ["process-fix", "partial-node", "wiring"],
    ["pr-land"],
    ["dod-verify"],
]
DEFAULT_ORDER = [list(g) for g in machinery_policy().tier_order]


@pytest.mark.parametrize(
    "tier_order", [TICKET_FIRST, TICKET_SHARES_MACHINERY, DEFAULT_ORDER]
)
def test_machinery_order_does_not_depend_on_enumeration_order(
    tier_order: list[list[str]],
) -> None:
    policy = _policy(tier_order)
    orders = {
        tuple(_dispatch(perm, policy)) for perm in itertools.permutations(CANDIDATES)
    }
    assert len(orders) == 1, orders


def test_ticket_before_machinery_policy_ranks_machinery_above_process_fix() -> None:
    policy = _policy(TICKET_FIRST)
    for perm in itertools.permutations((PROCESS_FIX, TICKET, MACHINERY)):
        assert _dispatch(perm, policy) == ["ticket", "machinery", "process-fix"]
        # One slot short: the scarce slot goes to machinery, never to the lower-tier process fix.
        assert _dispatch(perm, policy, max_lanes=2) == ["ticket", "machinery"]


def test_default_policy_still_ranks_machinery_first() -> None:
    policy = machinery_policy()
    assert _dispatch(CANDIDATES, policy)[0] == "machinery"
