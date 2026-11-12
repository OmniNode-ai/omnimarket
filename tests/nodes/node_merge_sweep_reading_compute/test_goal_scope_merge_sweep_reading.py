# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23851: the sweep's reading admits goal changes only through node_goal_scope_admission_compute.

The reading holds no goal list. An open PR the goal-scope rule admits is read; one it leaves out is named
under ``goal_not_admitted`` and never read; one whose ticket could not be read, or that the request holds no
candidate for, is named under ``unread`` and never read. The control reads the same open PRs with no goal
scope and shows the filter is the rule's: removing it reads them all. The recorded PRs are the ones
the controller admitted by root at 16:12Z on 2026-11-11 and an unrelated one under no root.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.goal_scope_admission import (
    ModelGoalCandidate,
    ModelGoalScopeAdmissionRequest,
    ModelGoalScopeAdmissionResult,
    ModelGoalTicketRule,
    ModelTicketFacts,
)
from omnimarket.models.merge_sweep import ModelMergeSweepReadRequest
from omnimarket.nodes.node_merge_sweep_reading_compute.handlers import (
    handler_merge_sweep_read as reading_module,
)
from omnimarket.nodes.node_merge_sweep_reading_compute.handlers.handler_merge_sweep_read import (
    HandlerMergeSweepRead,
)

pytestmark = pytest.mark.unit

NOW = "2026-11-11T18:30:00Z"
BOUNDS = {
    "endpoint": "https://linear.invalid/graphql",
    "cache_seconds": 3600,
    "reads_per_tick": 40,
    "read_timeout_seconds": 5,
}
ROOT = "OMN-22478"
IN_GOAL = {"omnimarket#5600": "OMN-23844", "omnimarket#5605": "OMN-23842"}
OUT_OF_GOAL = {"omnimarket#5001": "OMN-17427"}
UNREAD = {"omnimarket#5002": "OMN-30000"}
NO_CANDIDATE = "omnimarket#5003"
FACTS = {
    "OMN-23844": ModelTicketFacts(parent="OMN-23800"),
    "OMN-23842": ModelTicketFacts(parent="OMN-23800"),
    "OMN-23800": ModelTicketFacts(parent=ROOT),
    "OMN-17427": ModelTicketFacts(parent=None),
}


def _goal(**over: Any) -> ModelGoalScopeAdmissionRequest:
    cands = {
        ref: ModelGoalCandidate(title=f"fix({t}): recorded")
        for ref, t in {**IN_GOAL, **OUT_OF_GOAL, **UNREAD}.items()
    }
    return ModelGoalScopeAdmissionRequest(
        candidates=cands,
        rule=ModelGoalTicketRule(roots=frozenset({ROOT}), max_depth=6, **BOUNDS),
        ticket_facts=FACTS,
        unread_tickets={"OMN-30000": "Linear unreadable for OMN-30000: URLError"},
        **over,
    )


def _request(goal: ModelGoalScopeAdmissionRequest | None) -> ModelMergeSweepReadRequest:
    refs = [*IN_GOAL, *OUT_OF_GOAL, *UNREAD, NO_CANDIDATE]
    return ModelMergeSweepReadRequest(
        now=NOW,
        fleet=["omnimarket"],
        open_prs=[
            {
                "repo": "omnimarket",
                "number": int(ref.split("#")[1]),
                "title": "x",
                "base": "dev",
                "head_ref": f"b{ref}",
                "draft": True,
            }
            for ref in refs
        ],
        goal_scope=goal,
    )


def test_goal_scope_reading_reads_only_what_the_compute_admits() -> None:
    read = HandlerMergeSweepRead().handle(_request(_goal()))
    assert read.open_prs == len(IN_GOAL)
    assert read.goal_admitted == {
        "omnimarket#5600": "ticket:OMN-23844<OMN-23800<OMN-22478",
        "omnimarket#5605": "ticket:OMN-23842<OMN-23800<OMN-22478",
    }
    assert set(read.goal_not_admitted) == set(OUT_OF_GOAL)
    assert "under no goal root" in read.goal_not_admitted["omnimarket#5001"]


def test_goal_scope_reading_names_an_unread_ticket_and_a_missing_candidate_and_reads_neither() -> (
    None
):
    read = HandlerMergeSweepRead().handle(_request(_goal()))
    why = {u.field: u.why for u in read.unread if u.field.startswith("goal_scope:")}
    assert why["goal_scope:omnimarket#5002"].startswith("Linear unreadable")
    assert why["goal_scope:omnimarket#5003"] == (
        "the request carries no goal-scope candidate for it"
    )
    assert "omnimarket#5002" not in read.goal_admitted
    assert read.open_prs == len(IN_GOAL)


def test_goal_scope_failing_control_without_a_goal_scope_the_same_prs_are_all_read() -> (
    None
):
    read = HandlerMergeSweepRead().handle(_request(None))
    assert read.open_prs == len(IN_GOAL) + len(OUT_OF_GOAL) + len(UNREAD) + 1
    assert read.goal_admitted == {}
    assert read.goal_not_admitted == {}


def test_goal_scope_failing_control_a_reading_that_falls_back_to_a_list_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reading that decides from a list of its own, whatever the rule says, reads the PR the rule leaves out."""

    class ListFallback:
        def handle(
            self, request: ModelGoalScopeAdmissionRequest
        ) -> ModelGoalScopeAdmissionResult:
            from omnimarket.models.goal_scope_admission import ModelGoalAdmission

            return ModelGoalScopeAdmissionResult(
                admitted={
                    ref: ModelGoalAdmission(via="ticket", label="list")
                    for ref in request.candidates
                }
            )

    monkeypatch.setattr(reading_module, "HandlerGoalScopeAdmission", ListFallback)
    read = HandlerMergeSweepRead().handle(_request(_goal()))
    assert read.goal_not_admitted == {}
    assert read.open_prs != len(IN_GOAL), (
        "the list fallback read the PR the rule leaves out"
    )
