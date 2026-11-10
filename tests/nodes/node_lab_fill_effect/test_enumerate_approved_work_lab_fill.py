# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Approved-work enumerate chains (OMN-23250): row validation, skip order, per-stage floors, named failures.

The clock is scripted: each fake read advances it by its planted cost, or by the budget it was given
and then raises LabFillSourceCutError when the cost is more.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_lab_fill_effect.handlers import HandlerLabFillApprovedWork
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_approved_work import (
    row_is_valid,
    skip_reason,
)
from omnimarket.nodes.node_lab_fill_effect.handlers.helpers_lab_fill_enumerate import (
    acceptance_references,
    declared_repository,
)
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillApprovedWorkRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    LabFillApprovedTicket,
    LabFillMergedHistory,
    LabFillPortError,
    LabFillPrFacts,
    LabFillSourceCutError,
)

from .fakes import FakeClock

KINDS = ("process-fix", "partial-node", "wiring")
BASES = ("linear-state", "acceptance-on-main", "merged-history", "duplicate")


def _row(n: int, **over: Any) -> dict[str, object]:
    row: dict[str, object] = {
        "id": f"row-{n}",
        "kind": "process-fix",
        "ticket": f"OMN-{n}",
        "repo": "omnimarket",
        "goal": f"goal {n}",
        "acceptance_check": "uv run pytest tests/nodes/x.py",
        "added_by": "lane",
        "added_at": "2026-11-01T00:00:00Z",
    }
    row.update(over)
    return row


def _ticket(n: int, state: str = "started", **over: Any) -> LabFillApprovedTicket:
    base: dict[str, Any] = {
        "ticket": f"OMN-{n}",
        "state_type": state,
        "assignee_id": "",
        "updated_at": "2026-11-02T00:00:00Z",
        "description": "Repo: omnimarket.",
        "blockers": (),
        "prs": (),
        "merged_implementations": (),
    }
    return replace(LabFillApprovedTicket(**base), **over)


def _request(**over: Any) -> ModelLabFillApprovedWorkRequest:
    base: dict[str, object] = {
        "path": "approved.json",
        "kinds": KINDS,
        "done_bases": BASES,
        "fleet_repos": ("omnimarket", "omnibase_infra"),
        "max_rows": 500,
        "budget_s": 60.0,
        "stage_floor_s": 10.0,
    }
    base.update(over)
    return ModelLabFillApprovedWorkRequest.model_validate(base)


@dataclass
class World:
    clock: FakeClock
    rows: Sequence[object] = ()
    rows_error: str = ""
    prs: LabFillPrFacts = field(default_factory=lambda: LabFillPrFacts(frozenset(), {}))
    prs_error: str = ""
    history: LabFillMergedHistory = field(
        default_factory=lambda: LabFillMergedHistory({}, frozenset())
    )
    history_cost: float = 1.0
    nodes: Mapping[str, LabFillApprovedTicket] = field(default_factory=dict)
    states_cost: float = 1.0
    present: frozenset[str] = frozenset({"uv", "tests/nodes/x.py"})
    budgets: dict[str, float] = field(default_factory=dict)

    def _spend(self, stage: str, cost: float, budget: float) -> None:
        self.budgets[stage] = budget
        if cost > budget:
            self.clock.t += budget
            raise LabFillSourceCutError(f"{stage} cut after {budget} s")
        self.clock.t += cost

    def read_rows(self, path: str) -> Sequence[object]:
        if self.rows_error:
            raise LabFillPortError(self.rows_error)
        return self.rows

    def read_prs(self) -> LabFillPrFacts:
        if self.prs_error:
            raise LabFillPortError(self.prs_error)
        return self.prs

    def read_history(
        self, tickets: Sequence[str], budget_s: float
    ) -> LabFillMergedHistory:
        self._spend("merged-history", self.history_cost, budget_s)
        return self.history

    def read_tickets(
        self, tickets: Sequence[str], budget_s: float
    ) -> Mapping[str, LabFillApprovedTicket]:
        self._spend("approved-states", self.states_cost, budget_s)
        return self.nodes

    def probe(self, ref: str, repo: str) -> bool:
        return ref in self.present

    def handler(self) -> HandlerLabFillApprovedWork:
        return HandlerLabFillApprovedWork(
            rows=self.read_rows,
            prs=self.read_prs,
            history=self.read_history,
            tickets=self.read_tickets,
            acceptance=self.probe,
            clock=self.clock,
        )


@pytest.fixture
def world() -> World:
    return World(clock=FakeClock())


def test_golden_chain_rows_judged_on_the_facts(world: World) -> None:
    world.rows = [
        _row(1),
        _row(2),
        _row(3),
        _row(4, blocked_until="2026-12-01"),
        "junk",
    ]
    world.prs = LabFillPrFacts(frozenset({"OMN-2"}), {"OMN-3": ("omnimarket#9",)})
    world.nodes = {f"OMN-{n}": _ticket(n) for n in (1, 2, 3, 4)}
    result = world.handler().handle(_request())
    assert result.state == "ok"
    assert result.seen == {"process-fix": 5}
    assert result.invalid == {"process-fix": 1}
    skips = {c.row["ticket"]: c.approved_skip for c in result.candidates}
    assert skips == {
        "OMN-1": "",
        "OMN-2": "done:pr-merged",
        "OMN-3": "owned:open-pr:omnimarket#9",
        "OMN-4": "blocked-until:2026-12-01",
    }
    first = result.candidates[0]
    assert first.acceptance_verified == ("uv", "tests/nodes/x.py")
    assert first.declared_repo == "omnimarket"
    assert len(first.row_hash) == 64


def test_skip_order_done_ticket_before_history_and_open_pr() -> None:
    row = _row(7, checked_at="2026-11-01T00:00:00Z")
    history = LabFillMergedHistory(
        {"OMN-7": ("omnimarket", "2026-11-05T00:00:00Z", "a" * 40)}, frozenset()
    )
    prs = LabFillPrFacts(frozenset(), {"OMN-7": ("omnimarket#1",)})
    assert (
        skip_reason(row, _ticket(7), prs, history)
        == "stale:merged-after-check:omnimarket@aaaaaaaaaaaa"
    )
    assert skip_reason(row, _ticket(7, "completed"), prs, history) == "done:ticket"
    assert skip_reason(row, None, prs, history) == "unreadable:ticket"
    unread = LabFillMergedHistory({}, frozenset({"omnimarket"}))
    assert (
        skip_reason(row, _ticket(7), prs, unread)
        == "unreadable:merged-history:omnimarket"
    )
    after = _row(7, checked_at="2026-11-09T00:00:00Z")
    assert skip_reason(after, _ticket(7), prs, history) == "owned:open-pr:omnimarket#1"


@pytest.mark.parametrize(
    "over",
    [
        {"ticket": "OMX-1"},
        {"id": "has space"},
        {"kind": "other"},
        {"goal": "two\nlines"},
        {"added_at": "yesterday"},
        {"blocked_on": []},
        {"blocked_on": ["OMN-1"]},  # blocks itself
        {"done": True},  # done with no evidence
        {"done_basis": "vibes"},
        {"host": "bad host"},
    ],
)
def test_error_chain_invalid_row_is_counted_not_read(over: dict[str, object]) -> None:
    assert row_is_valid(_row(1, **over), set(), KINDS, BASES) is False
    assert row_is_valid(_row(1), set(), KINDS, BASES) is True
    assert row_is_valid(_row(1), {"row-1"}, KINDS, BASES) is False  # duplicate id


def test_error_chain_unreadable_file_names_its_stage(world: World) -> None:
    world.rows_error = "invalid list"
    result = world.handler().handle(_request())
    assert (result.state, result.failed_stage, result.candidates) == (
        "unreadable",
        "approved-rows",
        (),
    )


def test_error_chain_unreadable_watcher_is_named_not_empty(world: World) -> None:
    world.rows = [_row(1)]
    world.prs_error = "unreadable-from-snapshot: watcher freshness/schema"
    result = world.handler().handle(_request())
    assert (result.state, result.failed_stage) == ("unreadable", "pr-facts")
    assert "unreadable-from-snapshot" in result.detail


def test_error_chain_empty_list_reads_empty_and_oversize_reads_unreadable(
    world: World,
) -> None:
    assert world.handler().handle(_request()).state == "empty"
    world.rows = [_row(n) for n in range(1, 5)]
    result = world.handler().handle(_request(max_rows=3))
    assert (result.state, result.failed_stage) == ("unreadable", "approved-rows")


def test_ac1_slow_history_cannot_starve_the_state_read(world: World) -> None:
    """The history read spends everything it is given; the state read still has its own floor."""
    world.rows = [_row(1)]
    world.nodes = {"OMN-1": _ticket(1)}
    world.history_cost = 1000.0
    result = world.handler().handle(_request(budget_s=60.0, stage_floor_s=10.0))
    assert (result.state, result.failed_stage) == ("stale", "merged-history")
    # The cut stage was given what is left less the state stage's floor.
    assert world.budgets["merged-history"] == 50.0


def test_ac1_failing_control_without_floors_the_later_stage_has_nothing(
    world: World,
) -> None:
    """With the floor at 0 (the old shared budget) the history read takes the lot and the state read gets 0 s."""
    world.rows = [_row(1)]
    world.nodes = {"OMN-1": _ticket(1)}
    world.history_cost = 59.0
    world.handler().handle(_request(budget_s=60.0, stage_floor_s=0.0))
    floorless = world.budgets["approved-states"]
    world2 = World(
        clock=FakeClock(),
        rows=[_row(1)],
        nodes={"OMN-1": _ticket(1)},
        history_cost=49.0,
    )
    world2.handler().handle(_request(budget_s=60.0, stage_floor_s=10.0))
    assert floorless < 10.0 <= world2.budgets["approved-states"]


def test_states_stage_cut_marks_stale_with_no_candidates(world: World) -> None:
    world.rows = [_row(1)]
    world.states_cost = 1000.0
    result = world.handler().handle(_request())
    assert (result.state, result.failed_stage, result.candidates) == (
        "stale",
        "approved-states",
        (),
    )


def test_unwired_host_refuses_by_name() -> None:
    with pytest.raises(LabFillPortError, match="enumerate_lab_fill_approved_work"):
        HandlerLabFillApprovedWork().handle(_request())


def test_request_refuses_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ModelLabFillApprovedWorkRequest.model_validate(
            {**_request().model_dump(), "x": 1}
        )


def test_helpers_read_acceptance_references_and_repo_lines() -> None:
    assert acceptance_references("uv run pytest tests/a/b.py then curl http://x") == (
        "uv",
        "curl",
        "tests/a/b.py",
    )
    assert (
        declared_repository("**Repo:** omnimarket.\nbody", {"omnimarket"})
        == "omnimarket"
    )
    assert (
        declared_repository(
            "Repo omnimarket\nRepo omnibase_infra", {"omnimarket", "omnibase_infra"}
        )
        == ""
    )
    assert declared_repository("| Repo | PR |", {"omnimarket"}) == ""
