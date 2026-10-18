# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Idle lab slots take implementation-merged tickets as DoD-verify lanes (OMN-17427).

The lab-fill run of 2026-10-17T1730Z had 9 free slots, 2 eligible candidates and 41 candidates
skipped implementation-merged, and dispatched nothing. A ticket whose implementation merged is
unverified work, not finished work: the slots ordinary and pr-land work leave idle take those
tickets as DoD-verify lanes, one per ticket, gated by the same attempt history as other tickets.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_dispatch_plan import (
    HandlerLabFillDispatchPlan,
    dod_verify_item,
    skip_reason,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_lane_render import (
    HandlerLabFillLaneRender,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillCapacityResult,
    ModelLabFillDispatchPlanRequest,
    ModelLabFillHostCapacity,
    ModelLabFillPlanConfig,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models.model_lab_fill_lane_render import (
    ModelLabFillLaneRenderRequest,
    ModelLabFillRenderConfig,
)
from omnimarket.nodes.node_lab_fill_selection_compute.handlers import (
    HandlerLabFillSelection,
)
from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    EnumLabFillSkipReason,
    ModelLabFillCandidateInput,
    ModelLabFillDeployment,
    ModelLabFillSelectionRequest,
    ModelLabFillSelectionResult,
)

pytestmark = pytest.mark.unit

NOW = "2026-10-17T17:30:00Z"
RUN_KEY = "2026-10-17T1730Z"
DEPLOYMENT = ModelLabFillDeployment(("change_control",), ("registry",), ("docs",))
# The free lanes per host on the recorded STATUS row: hosts=h101:1,h202:4,h201:4, budget=9.
RECORDED_HOSTS = (("h101", 1), ("h202", 4), ("h201", 4))
RECORDED_FREE = 9
RECORDED_ELIGIBLE = 2
RECORDED_MERGED = 41


def _merged(n: int, **over: Any) -> ModelLabFillCandidateInput:
    fields: dict[str, Any] = {
        "key": f"ticket:OMN-{n}",
        "kind": "ticket",
        "ticket": f"OMN-{n}",
        "merged_prs": (f"omnimarket#{n}",),
    }
    fields.update(over)
    return ModelLabFillCandidateInput(**fields)


def _open(n: int) -> ModelLabFillCandidateInput:
    return ModelLabFillCandidateInput(
        key=f"ticket:OMN-{n}", kind="ticket", ticket=f"OMN-{n}"
    )


def _select(
    candidates: tuple[ModelLabFillCandidateInput, ...],
    *,
    idle_slots: int,
    ledger_lines: tuple[str, ...] = (),
) -> ModelLabFillSelectionResult:
    return HandlerLabFillSelection().handle(
        ModelLabFillSelectionRequest(
            now=NOW,
            candidates=candidates,
            ledger_lines=ledger_lines,
            deployment=DEPLOYMENT,
            idle_slots=idle_slots,
        )
    )


def _capacity(hosts: tuple[tuple[str, int], ...]) -> ModelLabFillCapacityResult:
    rows = tuple(
        ModelLabFillHostCapacity(
            name=name,
            lanes=lanes,
            idle_lanes=lanes,
            runner_slots=lanes,
            cap_bound=False,
            codex=False,
            claude=True,
            running=0,
            load_per_core=0.1,
            reason="",
        )
        for name, lanes in hosts
    )
    free = sum(lanes for _, lanes in hosts)
    return ModelLabFillCapacityResult(hosts=rows, free=free, budget=free)


def _config(max_lanes: int = 8) -> ModelLabFillPlanConfig:
    return ModelLabFillPlanConfig(
        parent_ticket="OMN-17427", milestone="m", run_key=RUN_KEY, max_lanes=max_lanes
    )


def _enumerated(n: int, kind: str = "ticket") -> dict[str, object]:
    return {
        "kind": kind,
        "ticket": f"OMN-{n}",
        "title": f"work {n}",
        "under_parent": True,
        "milestone": "m",
        "repo": "omnimarket",
    }


def _plan(
    candidates: list[dict[str, object]],
    hosts: tuple[tuple[str, int], ...] = (("h201", 4),),
    max_lanes: int = 8,
) -> Any:
    return HandlerLabFillDispatchPlan().handle(
        ModelLabFillDispatchPlanRequest(
            candidates=tuple(candidates),
            capacity=_capacity(hosts),
            config=_config(max_lanes),
        )
    )


def _claim(lane: str, ticket: str, ts: str = "2026-10-17T16:00:00Z") -> str:
    return f"{ts} | CLAIM | lane={lane} | ticket={ticket} | actor=x | scope"


def _terminal(lane: str, ticket: str, outcome: str, ts: str) -> str:
    return f"{ts} | TERMINAL | lane={lane} | ticket={ticket} | outcome={outcome} | done"


def test_merged_tickets_fill_the_slots_ordinary_work_leaves_idle() -> None:
    result = _select((_open(1), _merged(2), _merged(3), _merged(4)), idle_slots=3)

    assert [d.reason for d in result.decisions] == [
        None,
        *[EnumLabFillSkipReason.IMPLEMENTATION_MERGED] * 3,
    ]
    assert [(d.ticket, d.detail) for d in result.dod_verify] == [
        ("OMN-2", "omnimarket#2"),
        ("OMN-3", "omnimarket#3"),
    ]


def test_no_idle_slot_takes_no_dod_verify_work() -> None:
    assert _select((_merged(2),), idle_slots=0).dod_verify == ()
    assert _select((_open(1), _merged(2)), idle_slots=1).dod_verify == ()


def test_one_dod_verify_lane_per_ticket() -> None:
    twice = (_merged(2), _merged(2, key="defect:OMN-2", kind="defect"))
    assert [d.ticket for d in _select(twice, idle_slots=4).dod_verify] == ["OMN-2"]


def test_an_open_verify_lane_owns_its_ticket() -> None:
    lines = (_claim("lab-fill-verify-omn2-10171710", "OMN-2"),)
    result = _select((_merged(2), _merged(3)), idle_slots=4, ledger_lines=lines)
    assert [d.ticket for d in result.dod_verify] == ["OMN-3"]


def test_a_blocked_verify_lane_waits_out_the_cooldown() -> None:
    lane = "lab-fill-verify-omn2-10171510"
    blocked = (
        _claim(lane, "OMN-2", "2026-10-17T15:10:00Z"),
        _terminal(lane, "OMN-2", "blocked", "2026-10-17T15:40:00Z"),
    )
    assert _select((_merged(2),), idle_slots=1, ledger_lines=blocked).dod_verify == ()
    old = (
        _claim(lane, "OMN-2", "2026-10-17T05:10:00Z"),
        _terminal(lane, "OMN-2", "blocked", "2026-10-17T05:40:00Z"),
    )
    assert [
        d.ticket
        for d in _select((_merged(2),), idle_slots=1, ledger_lines=old).dod_verify
    ] == ["OMN-2"]


def test_dod_verify_ranks_after_every_other_kind_and_names_its_lane() -> None:
    plan = _plan(
        [dod_verify_item(_enumerated(2), "omnimarket#2"), _enumerated(9)],
        hosts=(("h201", 1),),
    )

    assert [(i.kind, i.ticket, i.pr) for i in plan.dispatch] == [
        ("ticket", "OMN-9", "")
    ]
    assert [(d.id, d.reason) for d in plan.deferred] == [("OMN-2", "over-cap")]
    item = _plan([dod_verify_item(_enumerated(2), "omnimarket#2")]).dispatch[0]
    assert (item.kind, item.lane, item.engine) == (
        "dod-verify",
        "lab-fill-verify-omn2-10171730",
        "sonnet",
    )
    assert item.title.startswith("implementation merged in omnimarket#2: ")


def test_dod_verify_is_scoped_as_the_kind_it_was_enumerated_as() -> None:
    cfg = _config()
    outside = {**_enumerated(2), "under_parent": False}
    assert skip_reason(dod_verify_item(outside, "x#1"), cfg, set()) == "out-of-scope"
    assert (
        skip_reason(dod_verify_item(_enumerated(2), "x#1"), cfg, {"OMN-2"}) == "fenced"
    )
    unknown = {**dod_verify_item(_enumerated(2), "x#1"), "origin_kind": "bogus"}
    assert skip_reason(unknown, cfg, set()) == "unknown-kind"


def test_an_ordinary_lane_for_the_same_ticket_wins_over_dod_verify() -> None:
    plan = _plan([dod_verify_item(_enumerated(2), "omnimarket#2"), _enumerated(2)])
    assert [(i.kind, i.ticket) for i in plan.dispatch] == [("ticket", "OMN-2")]


def test_the_dod_verify_brief_closes_only_on_a_passing_receipt() -> None:
    item = _plan([dod_verify_item(_enumerated(2), "omnimarket#2")]).dispatch[0]
    brief = (
        HandlerLabFillLaneRender()
        .handle(
            ModelLabFillLaneRenderRequest(
                item=item,
                config=ModelLabFillRenderConfig(
                    pillar="all",
                    parent_lane="orchestrator",
                    parent_ticket="OMN-17427",
                    run_key=RUN_KEY,
                    authority_ruling="2026-10-14T21:46:25Z",
                    authority_lane="orchestrator",
                ),
            )
        )
        .brief
    )

    assert "TASK: Verify and close OMN-2." in brief
    assert "implementation merged in omnimarket#2" in brief
    assert "/onex:dod_verify" in brief
    assert "never flip a ticket Done" not in brief
    assert "Done only on a passing /onex:dod_verify receipt" in brief


def test_replay_of_the_1730z_run_dispatches_at_least_the_free_slot_count() -> None:
    eligible = tuple(_open(1000 + i) for i in range(RECORDED_ELIGIBLE))
    merged = tuple(_merged(2000 + i) for i in range(RECORDED_MERGED))
    result = _select(eligible + merged, idle_slots=RECORDED_FREE)
    assert sum(d.reason is None for d in result.decisions) == RECORDED_ELIGIBLE
    assert len(result.dod_verify) == RECORDED_FREE - RECORDED_ELIGIBLE

    enumerated = {d.ticket: _enumerated(int(d.ticket[4:])) for d in result.decisions}
    candidates = [enumerated[c.ticket] for c in eligible]
    before = _plan(candidates, RECORDED_HOSTS, max_lanes=RECORDED_FREE)
    candidates += [
        dod_verify_item(enumerated[d.ticket], d.detail) for d in result.dod_verify
    ]
    after = _plan(candidates, RECORDED_HOSTS, max_lanes=RECORDED_FREE)

    assert len(before.dispatch) == RECORDED_ELIGIBLE
    assert len(after.dispatch) >= RECORDED_FREE
    assert sorted({i.host for i in after.dispatch}) == ["h101", "h201", "h202"]
