# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23851: the node returns the plugin's admitted and unread sets on the plugin's own recorded cases.

``fixtures/recorded_cases.json`` copies the recorded cases of the plugin's goal-scope tests
(test_goal_scope_ticket_rule, test_landing_ticket_project_scope, test_scope_parent_chain) with their ticket
ancestry as facts; its expected sets are what the plugin's ``admit_by_scope`` returned over the same facts. No
test here imports the plugin. Each refusal has its failing control: the same cases against a walk with the
ancestors removed, and against a read that admits what it could not read.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.goal_scope_admission import (
    ModelGoalCandidate,
    ModelGoalScopeAdmissionRequest,
    ModelGoalTicketRule,
    ModelTicketFacts,
)
from omnimarket.nodes.node_goal_scope_admission_compute.handlers import (
    handler_goal_scope_admission as handler_module,
)
from omnimarket.nodes.node_goal_scope_admission_compute.handlers.handler_goal_scope_admission import (
    HandlerGoalScopeAdmission,
)
from omnimarket.nodes.node_goal_scope_admission_compute.tests.support import (
    CASES,
    READ_BOUNDS,
    request_of,
)

pytestmark = pytest.mark.unit

HANDLER = HandlerGoalScopeAdmission()


def _admitted_and_unread(
    case: dict[str, Any],
) -> tuple[dict[str, list[str]], list[str]]:
    result = HANDLER.handle(request_of(case))
    return (
        {ref: [v.via, v.label] for ref, v in sorted(result.admitted.items())},
        sorted(result.unread),
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: str(c["name"]))
def test_recorded_case_matches_the_plugin_admitted_and_unread_sets(
    case: dict[str, Any],
) -> None:
    admitted, unread = _admitted_and_unread(case)
    assert admitted == case["expected"]["admitted"]
    assert unread == sorted(case["expected"]["unread"])


@pytest.mark.parametrize("case", CASES, ids=lambda c: str(c["name"]))
def test_every_candidate_is_under_exactly_one_answer_with_a_reason(
    case: dict[str, Any],
) -> None:
    result = HANDLER.handle(request_of(case))
    answers = [set(result.admitted), set(result.not_admitted), set(result.unread)]
    assert set().union(*answers) == set(case["candidates"])
    assert sum(len(a) for a in answers) == len(case["candidates"])
    assert all(reason for reason in {**result.not_admitted, **result.unread}.values())


def test_the_fixture_holds_every_kind_of_answer() -> None:
    kinds = {v[0] for c in CASES for v in c["expected"]["admitted"].values()} | {
        "unread" for c in CASES if c["expected"]["unread"]
    }
    assert kinds == {"ticket", "project", "unread"}
    assert any(
        "<" in v[1] for c in CASES for v in c["expected"]["admitted"].values()
    ), "no case admits through a chain of more than one ticket"


def test_failing_control_without_the_ancestor_walk_the_recorded_cases_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ancestor walk removed (the root test on the ticket itself only): the recorded cases no longer match."""
    original = handler_module.ticket_scope_verdict

    def no_walk(
        ticket: str, roots: Any, scope: Any, max_depth: int, facts_of: Any
    ) -> Any:
        return original(ticket, roots, scope, 0, facts_of)

    monkeypatch.setattr(handler_module, "ticket_scope_verdict", no_walk)
    differing = [
        c["name"]
        for c in CASES
        if _admitted_and_unread(c)
        != (c["expected"]["admitted"], sorted(c["expected"]["unread"]))
    ]
    assert differing, "removing the ancestor walk changed no recorded case"
    assert "recorded_2026-11-06_on_their_tickets_alone" in differing


def test_failing_control_a_read_that_admits_the_unread_differs_from_the_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treating a ticket nobody could read as a yes would admit changes the plugin holds as unread."""
    unread_case = next(
        c for c in CASES if c["name"] == "unreadable_linear_holds_everything_unread"
    )
    assert _admitted_and_unread(unread_case) == ({}, sorted(unread_case["candidates"]))

    def admit_unread(
        ticket: str, roots: Any, scope: Any, max_depth: int, facts_of: Any
    ) -> Any:
        return "ticket", ticket

    monkeypatch.setattr(handler_module, "ticket_scope_verdict", admit_unread)
    assert _admitted_and_unread(unread_case) != ({}, sorted(unread_case["candidates"]))


def test_an_unread_ticket_is_never_admitted_even_when_a_listed_project_is_read_later_on_the_chain() -> (
    None
):
    """A parent read that failed leaves the change unread; no facts for the ticket at all is unread, never a no."""
    request = ModelGoalScopeAdmissionRequest(
        candidates={"r#1": ModelGoalCandidate(title="fix(OMN-9): x")},
        rule=ModelGoalTicketRule(
            roots=frozenset({"OMN-1"}), max_depth=6, **READ_BOUNDS
        ),
        ticket_facts={},
    )
    result = HANDLER.handle(request)
    assert result.admitted == {}
    assert result.not_admitted == {}
    assert result.unread == {"r#1": "no facts were supplied for OMN-9"}


def test_a_partial_fact_is_read_not_guessed() -> None:
    """A parent known only as bridged state does not cover a parent walk's project question."""
    facts = ModelTicketFacts(parent="OMN-1", parent_known=False)
    request = ModelGoalScopeAdmissionRequest(
        candidates={"r#1": ModelGoalCandidate(title="fix(OMN-9): x")},
        rule=ModelGoalTicketRule(
            roots=frozenset({"OMN-1"}), max_depth=6, **READ_BOUNDS
        ),
        ticket_facts={"OMN-9": facts},
    )
    assert list(HANDLER.handle(request).unread) == ["r#1"]


def test_no_ticket_and_no_rule_are_named_not_admitted_with_a_reason() -> None:
    request = ModelGoalScopeAdmissionRequest(
        candidates={
            "r#1": ModelGoalCandidate(title="a title with no ticket"),
            "r#2": ModelGoalCandidate(title="fix(OMN-9): x"),
        }
    )
    result = HANDLER.handle(request)
    assert result.not_admitted == {
        "r#1": "declares no ticket",
        "r#2": "no goal rule or ticket scope is installed",
    }
    assert result.admitted == {}
    assert result.unread == {}
