# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The machinery tier of the lab-fill plan (OMN-22413).

Built from the lab-idle row lab-fill wrote at 2026-11-03T20:37:51Z, where twelve free slots
(h051:11, h053:1) went unused because every tier read eligible=0, and from a fixture window of
FRICTION rows and throughput-tick lines. The plan ranks open machinery classes ahead of plan
and feature work; the window, floor, tier order and priority class are contract data.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_dispatch_plan import (
    HandlerLabFillDispatchPlan,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillCapacityResult,
    ModelLabFillDispatchPlanRequest,
    ModelLabFillHostCapacity,
    ModelLabFillPlanConfig,
)

from .test_golden_chain_lab_fill_plan_compute import _contract

pytestmark = pytest.mark.unit

# The facts the 2026-11-03T20:37:51Z lab-idle row records: idle hosts and free slots, and every
# recorded tier at eligible=0 (the plan-stalled source saw 65 tickets, the others 139, 39 and 22).
RECORDED_ROW = (
    "2026-11-03T20:37:51Z | FRICTION | lane=lab-fill | class=lab-idle | "
    "idle-hosts=h051:11,h053:1 | free-slots=12 | tier-reasons="
    "pr-red:seen=139:eligible=0,defect:seen=39:eligible=0,"
    "ticket:seen=22:eligible=0,plan-stalled:seen=65:eligible=0"
)
OBSERVED_AT = "2026-11-03T20:37:51Z"
NODE = Path(__file__).parents[3] / "src/omnimarket/nodes/node_lab_fill_plan_compute"


def _recorded_hosts() -> tuple[ModelLabFillHostCapacity, ...]:
    cell = re.search(r"idle-hosts=(\S+)", RECORDED_ROW)
    assert cell is not None
    hosts = []
    for part in cell[1].split(","):
        name, _, lanes = part.partition(":")
        hosts.append(
            ModelLabFillHostCapacity(
                name=name,
                lanes=int(lanes),
                idle_lanes=int(lanes),
                runner_slots=int(lanes),
                cap_bound=False,
                codex=True,
                claude=True,
                running=0,
                load_per_core=0.1,
                reason="",
            )
        )
    return tuple(hosts)


def _plan(candidates: list[object], **over: Any) -> Any:
    free = re.search(r"free-slots=(\d+)", RECORDED_ROW)
    assert free is not None
    cfg = {
        "parent_ticket": "OMN-17427",
        "milestone": "m",
        "run_key": "2026-11-03T2037Z",
        "max_lanes": 12,
    }
    return HandlerLabFillDispatchPlan().handle(
        ModelLabFillDispatchPlanRequest(
            candidates=tuple(candidates),
            capacity=ModelLabFillCapacityResult(
                hosts=_recorded_hosts(),
                free=int(free[1]),
                budget=int(free[1]),
            ),
            config=ModelLabFillPlanConfig(**{**cfg, **over}),
        )
    )


# A plan-stalled task as the fill enumerates it: a ticket candidate whose evidence is the plan.
PLAN_STALLED = {
    "kind": "ticket",
    "ticket": "OMN-100",
    "title": "plan task",
    "under_parent": True,
    "evidence": "plan beta/plans/2026-10-01-lab.md, priority high, idle 30 h",
}


def _friction(ts: str, **over: str) -> dict[str, str]:
    return {
        "ts": ts,
        "lane": "lab-fill",
        "class": "lab-idle",
        "guard": "",
        "reason": "",
        "ticket": "OMN-17427",
        **over,
    }


# A rolling-ledger window: lab-idle recurs five times today; a guard refusal recurs three times
# on one reason; one class occurs once; one lab-idle row is two days old.
FRICTION_WINDOW = [
    *(_friction(f"2026-11-03T{h:02d}:07:51Z") for h in (16, 17, 18, 19, 20)),
    *(
        _friction(
            f"2026-11-03T{h:02d}:30:00Z",
            **{
                "class": "guard-refusal",
                "guard": "worktree-guard",
                "reason": "stray-root",
            },
        )
        for h in (12, 13, 14)
    ),
    _friction("2026-11-03T15:00:00Z", **{"class": "one-off"}),
    _friction("2026-11-01T09:00:00Z"),
]
TICK_LINES = [
    {"tick": "2026-11-03T18:00:00Z", "key": "lab-fill.last-fire", "status": "MISSING"},
    {"tick": "2026-11-03T19:00:00Z", "key": "lab-fill.last-fire", "status": "UNKNOWN"},
    {"tick": "2026-11-03T19:00:00Z", "key": "pr-watcher.tick-age", "status": "UNKNOWN"},
    {"tick": "2026-11-03T20:00:00Z", "key": "queue.depth", "status": "OK"},
    {"tick": "2026-11-03T20:00:00Z", "key": "queue.depth", "status": "OK"},
]
MACHINERY_TICKET = {
    "identifier": "OMN-22000",
    "title": "Guard refuses a valid worktree",
    "description": "created-by-lane: x\n\nClass: machinery\n\nbody",
    "state_type": "unstarted",
}
FEATURE_TICKET = {
    "identifier": "OMN-22001",
    "title": "A feature",
    "description": "Class: feature",
    "state_type": "unstarted",
}


def _machinery(**over: Any) -> Any:
    from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
        HandlerLabFillMachineryCandidates,
    )
    from omnimarket.nodes.node_lab_fill_plan_compute.models import (
        ModelLabFillMachineryCandidatesRequest,
    )

    request = ModelLabFillMachineryCandidatesRequest.model_validate(
        {
            "observed_at": OBSERVED_AT,
            "friction_rows": FRICTION_WINDOW,
            "tick_lines": TICK_LINES,
            "tickets": [MACHINERY_TICKET, FEATURE_TICKET],
            "claims": [],
            **over,
        }
    )
    return HandlerLabFillMachineryCandidates().handle(request)


def test_lab_fill_machinery_tier_ranks_first() -> None:
    # Today's plan: the recorded state offers a plan-stalled task and no machinery candidate.
    today = _plan([PLAN_STALLED])
    assert [i.kind for i in today.dispatch] == ["ticket"]
    assert not any(i.kind == "machinery" for i in today.dispatch)

    found = _machinery()
    candidates = [c.model_dump() for c in found.candidates]
    classes = {c["machinery_key"] for c in candidates}
    # Recurring FRICTION classes at or above the floor, a tick key on two ticks, a Class ticket.
    assert "friction:lab-idle" in classes
    assert "friction:guard-refusal:worktree-guard:stray-root" in classes
    assert "tick:lab-fill.last-fire" in classes
    assert "ticket:OMN-22000" in classes
    # Below the floor, outside the window, a single tick, an OK line and a feature ticket are not.
    assert not any("one-off" in k or "pr-watcher" in k for k in classes)
    assert not any("queue.depth" in k or "OMN-22001" in k for k in classes)
    lab_idle = next(c for c in candidates if c["machinery_key"] == "friction:lab-idle")
    assert lab_idle["evidence"].startswith("5 FRICTION rows")

    plan = _plan([PLAN_STALLED, *candidates])
    assert plan.dispatch[0].kind == "machinery"
    assert plan.dispatch[-1].kind == "ticket"
    assert {i.kind for i in plan.dispatch[:-1]} == {"machinery"}
    assert len(plan.dispatch) == 5
    assert {i.priority_class for i in plan.dispatch if i.kind == "machinery"} == {
        "mvp-landing"
    }
    # The 12 recorded slots were h051:11 and h053:1; the machinery lanes land on them.
    assert {i.host for i in plan.dispatch} <= {"h051", "h053"}


def test_lab_fill_machinery_tier_wins_the_only_slot() -> None:
    candidates = [c.model_dump() for c in _machinery().candidates]
    plan = _plan([PLAN_STALLED, *candidates], max_lanes=1)
    assert [i.kind for i in plan.dispatch] == ["machinery"]
    assert [(d.id, d.reason) for d in plan.deferred if d.id == "OMN-100"] == [
        ("OMN-100", "over-cap")
    ]


def test_lab_fill_machinery_owned_skipped() -> None:
    claims = [
        {
            "lane": "fix-lab-idle-1100",
            "ticket": "OMN-22222",
            "text": "CLAIM | lane=fix-lab-idle-1100 | scope=class lab-idle fill break",
        },
        {
            "lane": "tick-owner",
            "ticket": "OMN-22223",
            "text": "CLAIM | lane=tick-owner",
        },
        {
            "lane": "ticket-owner",
            "ticket": "OMN-22000",
            "text": "CLAIM | lane=ticket-owner",
        },
    ]
    found = _machinery(claims=claims)
    keys = {c.machinery_key for c in found.candidates}
    assert "friction:lab-idle" not in keys
    assert "ticket:OMN-22000" not in keys
    assert "friction:guard-refusal:worktree-guard:stray-root" in keys
    skipped = {s.id: s.reason for s in found.skipped}
    assert skipped["friction:lab-idle"] == "owned:fix-lab-idle-1100"
    assert skipped["ticket:OMN-22000"] == "owned:ticket-owner"


def test_lab_fill_machinery_tier_contract_data() -> None:
    from omnimarket.nodes.node_lab_fill_plan_compute.handlers.helpers_machinery_policy import (
        machinery_policy,
    )
    from omnimarket.nodes.node_lab_fill_plan_compute.models import (
        ModelLabFillMachineryPolicy,
    )

    block = _contract()["config"]["lab_fill_plan"]["machinery"]
    assert block["priority_class"] == "mvp-landing"
    assert block["window_hours"] == 24
    assert block["friction_recurrence_floor"] == 3
    assert block["tick_recurrence_floor"] == 2
    assert block["tier_order"][0] == ["machinery"]
    assert ModelLabFillMachineryPolicy.model_validate(block) == machinery_policy()

    # None of the four values is a literal of the handlers: the model has no defaults and the
    # handlers hold neither the class name nor the window or floor.
    for name in ("priority_class", "window_hours", "friction_recurrence_floor"):
        assert ModelLabFillMachineryPolicy.model_fields[name].is_required()
    sources = "\n".join(
        (NODE / "handlers" / n).read_text(encoding="utf-8")
        for n in (
            "handler_lab_fill_machinery_candidates.py",
            "handler_lab_fill_dispatch_plan.py",
        )
    )
    assert "mvp-landing" not in sources
    assert "mvp_landing" not in sources

    # Changing the contract data changes the decision.
    base = dict(block)
    assert any(
        c.machinery_key == "friction:guard-refusal:worktree-guard:stray-root"
        for c in _machinery().candidates
    )
    stricter = ModelLabFillMachineryPolicy.model_validate(
        {**base, "friction_recurrence_floor": 4}
    )
    keys = {c.machinery_key for c in _machinery(policy=stricter).candidates}
    assert keys & {"friction:lab-idle"} == {"friction:lab-idle"}
    assert "friction:guard-refusal:worktree-guard:stray-root" not in keys
    narrower = ModelLabFillMachineryPolicy.model_validate({**base, "window_hours": 2})
    assert not any(
        k.startswith("friction:guard")
        for k in {c.machinery_key for c in _machinery(policy=narrower).candidates}
    )
    renamed = ModelLabFillMachineryPolicy.model_validate(
        {**base, "priority_class": "process-fix"}
    )
    assert {c.priority_class for c in _machinery(policy=renamed).candidates} == {
        "process-fix"
    }
    reordered = ModelLabFillMachineryPolicy.model_validate(
        {
            **base,
            "tier_order": [
                ["ticket"],
                ["machinery"],
                *[g for g in base["tier_order"][1:] if g != ["ticket"]],
            ],
        }
    )
    assert reordered.tier_order[0] == ("ticket",)
    candidates = [c.model_dump() for c in _machinery().candidates]
    plan = HandlerLabFillDispatchPlan().handle(
        ModelLabFillDispatchPlanRequest(
            candidates=(PLAN_STALLED, *candidates),
            capacity=ModelLabFillCapacityResult(
                hosts=_recorded_hosts(), free=12, budget=12
            ),
            config=ModelLabFillPlanConfig(
                parent_ticket="OMN-17427", milestone="m", run_key="2026-11-03T2037Z"
            ),
            machinery=reordered,
        )
    )
    assert plan.dispatch[0].kind == "ticket"


def test_lab_fill_machinery_lane_carries_the_priority_class() -> None:
    from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_launch import (
        runner_args,
    )
    from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
        HandlerLabFillLaneRender,
    )
    from omnimarket.nodes.node_lab_fill_plan_compute.models import (
        ModelLabFillLaneRenderRequest,
        ModelLabFillRenderConfig,
    )

    candidates = [c.model_dump() for c in _machinery().candidates]
    item = _plan(candidates).dispatch[0]
    assert item.kind == "machinery"
    assert item.ticket.startswith("OMN-")
    rendered = HandlerLabFillLaneRender().handle(
        ModelLabFillLaneRenderRequest(
            item=item,
            config=ModelLabFillRenderConfig(
                pillar="lab-fill",
                parent_lane="lab-fill",
                parent_ticket="OMN-17427",
                run_key="2026-11-03T2037Z",
                authority_ruling="2026-11-03T20:50:00Z",
                authority_lane="orchestrator-31d9",
            ),
        )
    )
    assert "factory machinery" in rendered.brief
    assert rendered.launch.priority_class == "mvp-landing"
    args = runner_args(rendered.launch, "/tmp/brief.md")
    assert args[args.index("--priority-class") + 1] == "mvp-landing"


def test_lab_fill_machinery_tier_reads_first_in_the_idle_row() -> None:
    from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
        HandlerLabFillStatus,
    )
    from omnimarket.nodes.node_lab_fill_plan_compute.models import (
        ModelLabFillStatusRequest,
    )

    found = _machinery(claims=[{"lane": "owner", "text": "class lab-idle"}])
    request = {
        "config": {
            "pillar": "lab-fill",
            "parent_lane": "lab-fill",
            "parent_ticket": "OMN-17427",
            "run_key": "2026-11-03T2037Z",
            "authority_ruling": "2026-11-03T20:50:00Z",
            "authority_lane": "orchestrator-31d9",
        },
        "capacity": {
            "hosts": [
                h.model_dump(by_alias=True, mode="json") for h in _recorded_hosts()
            ],
            "free": 12,
            "budget": 12,
        },
        "selection": {"budget": 12},
        "candidates": {
            "source": "enumerate",
            "source_diagnostics": {"machinery": found.diagnostic.model_dump()},
        },
        "observed_at": OBSERVED_AT,
    }
    idle = (
        HandlerLabFillStatus()
        .handle(ModelLabFillStatusRequest.model_validate(request))
        .idle
    )
    tiers = str(idle["friction_cells"]).split("tier-reasons=")[1]
    assert tiers.startswith("tier0-machinery:machinery:ok:seen=")
    assert ":eligible=3:" in tiers.split(";")[0]
    assert "owned:1" in tiers.split(";")[0]
