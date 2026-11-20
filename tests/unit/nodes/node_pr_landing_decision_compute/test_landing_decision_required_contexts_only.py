# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing decision merges on the base branch's required contexts only.

A check-run the base branch does not require stays visible in the tick and never holds a
merge, never counts as an external blocker and never feeds the external judgement. A required
red still holds, and required contexts that could not be read fail closed.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)

KEY = "OmniNode-ai/omnimarket#6241"
HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
CREATED_AT = "2026-11-19T00:00:00Z"
ADVISORY = "delegation-health-check / Delegation Health Check"
REQUIRED = ["CI Summary", "lint", "typecheck"]


def _facts(pr: dict[str, Any]) -> ModelLandingFacts:
    return ModelLandingFacts.model_validate(
        {
            "tick": 5,
            "observed_at": "2026-11-20T14:00:00Z",
            "prs": [pr],
            "state": {"last_tick": 4},
        }
    )


def _pr(**fields: Any) -> dict[str, Any]:
    return {
        "pr": KEY,
        "head_sha": HEAD,
        "state": "open",
        "ci": "red",
        "red_class": "product",
        "red_checks": [ADVISORY],
        "merge_state": "clean",
        "suspensions": [],
        "collaborator": False,
        "created_at": CREATED_AT,
        "required_contexts": REQUIRED,
        "passed_checks": REQUIRED,
        **fields,
    }


def _kinds(decision: ModelLandingDecision) -> list[str]:
    return [a.kind.value for a in decision.actions]


@pytest.mark.unit
def test_required_green_with_a_non_required_red_merges() -> None:
    decision = decide_landing(_facts(_pr()))
    assert _kinds(decision) == [EnumLandingActionKind.MERGE.value]
    assert decision.actions[0].subject == KEY
    assert decision.actions[0].head_sha == HEAD
    assert decision.degraded == ()
    assert decision.recorded_outcomes == ()


@pytest.mark.unit
def test_a_required_red_still_holds_and_names_only_the_required_red() -> None:
    decision = decide_landing(
        _facts(_pr(red_checks=[ADVISORY, "lint"], passed_checks=["CI Summary"]))
    )
    assert EnumLandingActionKind.MERGE.value not in _kinds(decision)
    assert decision.non_required_red[0].checks == (ADVISORY,)
    leased = [c for le in decision.next_state.leases for c in le.dispatch_red_checks]
    assert leased == ["lint"]


@pytest.mark.unit
def test_a_required_context_still_pending_holds_the_merge() -> None:
    decision = decide_landing(_facts(_pr(passed_checks=["CI Summary", "lint"])))
    assert EnumLandingActionKind.MERGE.value not in _kinds(decision)
    assert decision.non_required_red[0].checks == (ADVISORY,)


@pytest.mark.unit
def test_unreadable_required_contexts_fail_closed_and_say_why() -> None:
    decision = decide_landing(_facts(_pr(required_contexts=None, passed_checks=[])))
    assert EnumLandingActionKind.MERGE.value not in _kinds(decision)
    assert decision.non_required_red == ()
    assert [r.pr for r in decision.required_contexts_unread] == [KEY]
    assert decision.required_contexts_unread[0].red_checks == (ADVISORY,)
    assert "required contexts unreadable" in decision.required_contexts_unread[0].reason


@pytest.mark.unit
def test_the_tick_line_lists_the_non_required_reds() -> None:
    decision = decide_landing(_facts(_pr()))
    cells = decision.tick_cells()
    assert any(
        KEY in c and ADVISORY in c and c.startswith("non_required_red") for c in cells
    )
    unread = decide_landing(_facts(_pr(required_contexts=None, passed_checks=[])))
    assert any(
        c.startswith("required_contexts_unread") and KEY in c
        for c in unread.tick_cells()
    )
