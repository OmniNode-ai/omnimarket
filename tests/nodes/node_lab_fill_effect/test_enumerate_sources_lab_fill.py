# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enumerate chains (OMN-23250): per-stage budget floors, per-source stale marks, a young plans-today copy.

The clock is scripted: each fake read advances it by its planted cost, or by the budget it was
given and then raises LabFillSourceCutError when the cost is more.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_lab_fill_effect.handlers import HandlerLabFillSources
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillSourcesRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    LabFillListed,
    LabFillPortError,
    LabFillSourceCutError,
    LabFillTicketNode,
)

from .fakes import FakeClock

# Measured on h202 (OMN-23250): Linear snapshot 1.3 s, ruling_mechanisms.py 3.7 s,
# plans_today.py 14.8 s of which 13.8 s is the whole-ledger read.
PLANS_COST_S = 15.0
RULINGS_COST_S = 3.7
RESENDS_COST_S = 0.1
TICKETS_COST_S = 1.3

PLANS = LabFillListed(found=(("OMN-1", "plan a"), ("OMN-2", "plan b")))
RULINGS = LabFillListed(found=(("OMN-3", "RULING x"),))
RESENDS = LabFillListed(found=(("OMN-4", "resend"),), dropped=(("live-lane", 2),))


def _node(
    ticket: str,
    *,
    state_type: str = "started",
    has_children: bool = False,
    open_prs: tuple[str, ...] = (),
) -> LabFillTicketNode:
    return LabFillTicketNode(
        ticket=ticket,
        title=f"title {ticket}",
        state_type=state_type,
        has_children=has_children,
        open_prs=open_prs,
    )


@dataclass
class World:
    clock: FakeClock
    plans_cost: float = PLANS_COST_S
    rulings_cost: float = RULINGS_COST_S
    resends_cost: float = RESENDS_COST_S
    tickets_cost: float = TICKETS_COST_S
    copy_age: float | None = None
    copy_list: LabFillListed = PLANS
    nodes: Mapping[str, LabFillTicketNode] = field(default_factory=dict)
    rulings_error: str = ""
    ledger_reads: int = 0
    budgets: dict[str, float] = field(default_factory=dict)
    ticket_asks: list[Sequence[str]] = field(default_factory=list)

    def _spend(self, stage: str, cost: float, budget: float) -> None:
        self.budgets[stage] = budget
        if cost > budget:
            self.clock.t += budget
            raise LabFillSourceCutError(f"{stage} cut after {budget} s")
        self.clock.t += cost

    # ProtocolLabFillPlansCopy
    def age_min(self) -> float | None:
        return self.copy_age

    def load(self) -> LabFillListed:
        return self.copy_list

    def rebuild(self, budget_s: float) -> LabFillListed:
        self.ledger_reads += 1
        self._spend("plan-stalled", self.plans_cost, budget_s)
        self.copy_age = 0.0
        self.copy_list = PLANS
        return PLANS

    # ProtocolLabFillSourceReaders
    def read_rulings(self, budget_s: float) -> LabFillListed:
        if self.rulings_error:
            raise LabFillPortError(self.rulings_error)
        self._spend("ruling-pending", self.rulings_cost, budget_s)
        return RULINGS

    def read_resends(self, budget_s: float) -> LabFillListed:
        self._spend("lane-resend", self.resends_cost, budget_s)
        return RESENDS

    def read_tickets(
        self, tickets: Sequence[str], budget_s: float
    ) -> Mapping[str, LabFillTicketNode]:
        self.ticket_asks.append(tuple(tickets))
        self._spend("source-tickets", self.tickets_cost, budget_s)
        return self.nodes


def _nodes() -> dict[str, LabFillTicketNode]:
    return {t: _node(t) for t in ("OMN-1", "OMN-2", "OMN-3", "OMN-4")}


def _request(**kw: object) -> ModelLabFillSourcesRequest:
    base: dict[str, object] = {
        "budget_s": 20.0,
        "stage_floor_s": 4.0,
        "stage_cap_s": {},
        "plans_copy_max_age_min": 30.0,
        "plans_copy_stale_max_age_min": 360.0,
        "sources": ("plan-stalled", "ruling-pending", "lane-resend"),
    }
    base.update(kw)
    return ModelLabFillSourcesRequest.model_validate(base)


def _run(world: World, request: ModelLabFillSourcesRequest | None = None):
    return HandlerLabFillSources(world, world, world.clock).handle(
        request or _request()
    )


def _world(
    *,
    copy_age: float | None = None,
    plans_cost: float = PLANS_COST_S,
    tickets_cost: float = TICKETS_COST_S,
    rulings_error: str = "",
) -> World:
    return World(
        clock=FakeClock(),
        nodes=_nodes(),
        copy_age=copy_age,
        plans_cost=plans_cost,
        tickets_cost=tickets_cost,
        rulings_error=rulings_error,
    )


# ---- golden chain


def test_golden_every_source_ok_resolves_every_ticket() -> None:
    world = _world(copy_age=None, plans_cost=2.0)
    result = _run(world, _request(budget_s=60.0))
    assert [r.state for r in result.reads] == ["ok", "ok", "ok", "ok"]
    assert result.stale == ()
    assert result.unreadable == ()
    assert sorted(c.ticket for c in result.candidates) == [
        "OMN-1",
        "OMN-2",
        "OMN-3",
        "OMN-4",
    ]
    assert all(c.resolved for c in result.candidates)
    assert [(d.kind, d.reason, d.count) for d in result.dropped] == [
        ("lane-resend", "live-lane", 2)
    ]
    assert world.ticket_asks == [("OMN-1", "OMN-2", "OMN-3", "OMN-4")]


def test_golden_ticket_drops_are_counted_per_source() -> None:
    world = _world(copy_age=1.0)
    world.nodes = {
        "OMN-1": _node("OMN-1", state_type="completed"),
        "OMN-2": _node("OMN-2", has_children=True),
        "OMN-3": _node("OMN-3", open_prs=("omnimarket#1",)),
    }
    result = _run(world)
    assert [c.ticket for c in result.candidates] == []
    reasons = {(d.kind, d.reason): d.count for d in result.dropped}
    assert reasons[("plan-stalled", "done")] == 1
    assert reasons[("plan-stalled", "has-children")] == 1
    assert reasons[("ruling-pending", "open-pr")] == 1
    assert reasons[("lane-resend", "ticket-unreadable")] == 1


# ---- AC1: a slow plans-today read cannot starve source-tickets


def test_ac1_planted_15s_plans_read_still_leaves_source_tickets_its_budget() -> None:
    world = _world(copy_age=None)
    result = _run(world)
    assert world.ticket_asks, "source-tickets was never read"
    tickets = result.reads[-1]
    assert tickets.stage == "source-tickets"
    assert tickets.state == "ok"
    assert tickets.budget_s >= 4.0
    assert world.budgets["plan-stalled"] == pytest.approx(
        8.0
    )  # 20 s less 3 floors of 4 s


def test_ac1_control_no_floor_is_the_shared_budget_and_starves_source_tickets() -> None:
    """The named injected break: stage_floor_s=0 is the old shared budget."""
    world = _world(copy_age=None)
    result = _run(world, _request(stage_floor_s=0.0))
    assert result.reads[0].state == "ok"
    assert result.reads[-1].state == "stale"
    assert world.budgets["source-tickets"] < TICKETS_COST_S


# ---- AC2: a cut keeps the other lists, marks one source stale


def test_ac2_planted_timeout_on_one_source_marks_only_it_stale() -> None:
    world = _world(copy_age=None)
    result = _run(world)
    assert result.stale == ("plan-stalled",)
    assert result.unreadable == ()
    assert {c.kind for c in result.candidates} == {"ruling-pending", "lane-resend"}
    assert sorted(c.ticket for c in result.candidates) == ["OMN-3", "OMN-4"]


def test_ac2_cut_ticket_stage_keeps_every_list_and_marks_only_itself() -> None:
    world = _world(copy_age=1.0, tickets_cost=99.0)
    result = _run(world)
    assert result.stale == ("source-tickets",)
    assert sorted(c.ticket for c in result.candidates) == [
        "OMN-1",
        "OMN-2",
        "OMN-3",
        "OMN-4",
    ]
    assert not any(c.resolved for c in result.candidates)
    assert [r.state for r in result.reads[:3]] == ["ok", "ok", "ok"]


def test_ac2_cut_plans_rebuild_serves_an_older_copy_and_marks_stale() -> None:
    world = _world(copy_age=120.0)
    result = _run(world)
    plans = result.reads[0]
    assert plans.state == "stale"
    assert plans.from_copy_age_min == 120.0
    assert plans.tickets == ("OMN-1", "OMN-2")
    assert result.stale == ("plan-stalled",)
    assert "OMN-1" in {c.ticket for c in result.candidates}


# ---- AC3: a young copy is used with no whole-ledger read


def test_ac3_copy_younger_than_bound_is_used_with_zero_ledger_reads() -> None:
    world = _world(copy_age=29.0)
    result = _run(world)
    assert world.ledger_reads == 0
    assert result.reads[0].state == "ok"
    assert result.reads[0].from_copy_age_min == 29.0
    assert result.stale == ()
    assert result.elapsed_s == pytest.approx(
        RULINGS_COST_S + RESENDS_COST_S + TICKETS_COST_S
    )


def test_ac3_copy_older_than_bound_is_rebuilt() -> None:
    world = _world(copy_age=31.0, plans_cost=2.0)
    result = _run(world)
    assert world.ledger_reads == 1
    assert world.copy_age == 0.0
    assert result.reads[0].from_copy_age_min is None
    assert result.reads[0].state == "ok"


def test_ac3_control_every_run_without_a_copy_reads_the_ledger() -> None:
    world = _world(copy_age=None, plans_cost=2.0)
    _run(world, _request(budget_s=60.0))
    assert world.ledger_reads == 1
    _run(world, _request(budget_s=60.0))  # the first run saved a copy
    assert world.ledger_reads == 1


# ---- error chain


def test_error_unreadable_source_is_named_and_the_others_read() -> None:
    world = _world(copy_age=1.0, rulings_error="ruling-mechanisms exit 2")
    result = _run(world)
    assert result.unreadable == ("ruling-pending",)
    assert result.stale == ()
    assert result.reads[1].detail == "ruling-mechanisms exit 2"
    assert sorted(c.ticket for c in result.candidates) == ["OMN-1", "OMN-2", "OMN-4"]
    assert result.reads[0].state == "ok"


def test_error_request_that_cannot_hold_its_floors_is_refused() -> None:
    with pytest.raises(ValidationError, match="cannot hold"):
        _request(budget_s=10.0, stage_floor_s=4.0)
    with pytest.raises(ValidationError, match="unknown source"):
        _request(sources=("plan-stalled", "nonsense"))
    with pytest.raises(ValidationError, match="unknown stage cap"):
        _request(stage_cap_s={"nonsense": 1.0})


def test_error_no_listed_ticket_skips_the_ticket_read() -> None:
    world = _world(copy_age=1.0)
    world.copy_list = LabFillListed(found=())
    request = _request(sources=("plan-stalled",))
    result = _run(world, request)
    assert world.ticket_asks == []
    assert result.reads[-1].state == "empty"
    assert result.candidates == ()


# ---- the runtime builds the handler at boot from injectable parameters only


def test_boot_construction_without_ports_then_refuses_by_name() -> None:
    handler = HandlerLabFillSources()
    with pytest.raises(LabFillPortError, match="plans port is not injected"):
        handler.handle(_request())
