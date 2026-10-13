# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tier-1 sources of lab-fill's enumeration decide in the plan node (OMN-20668).

Golden chain: the stalled-plan and pending-ruling documents become source tickets, and the
tickets with their Linear and watcher facts become candidates, through the contract's own
bindings. Error chain: a document with no list, an unreadable value and a failed ticket or
watcher read are named unreadable, never empty; a bad request is refused by validation.
Parity: the expected answers were recorded by running the tier-1 block of the old enumerate
read (lab-fill.js) under python on stubbed tools, Linear and PR watcher.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
    HandlerLabFillSourceCandidates,
    HandlerLabFillSourceTickets,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillSourceCandidatesRequest,
    ModelLabFillSourceCandidatesResult,
    ModelLabFillSourceTicketsRequest,
    ModelLabFillSourceTicketsResult,
)

from .test_golden_chain_lab_fill_plan_compute import _contract, _resolve

FIXTURE = Path(__file__).parent / "fixtures" / "parity_cases_sources.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
SOURCE_OPERATIONS = (
    "collect_lab_fill_source_tickets",
    "classify_lab_fill_source_candidates",
)
OPERATIONS = {
    e["operation"]: _resolve(e)
    for e in _contract()["handler_routing"]["handlers"]
    if e["operation"] in SOURCE_OPERATIONS
}
PRIORITIES = ("urgent", "high")
KINDS = ("dispatch", "fix-red")


def _plan(ticket: str, **over: object) -> dict[str, object]:
    return {
        "path": "beta/plans/2026-10-01-lab.md",
        "verdict": "STALLED",
        "priority": "high",
        "next_action_kind": "dispatch",
        "next_action_ticket": ticket,
        "hours_idle": 30,
        **over,
    }


def _facts(ticket: str, **over: object) -> dict[str, object]:
    return {
        "identifier": ticket,
        "title": f"Ticket {ticket}",
        "updated_at": "2026-10-10T09:00:00Z",
        "state_type": "started",
        **over,
    }


def _tickets(**over: object) -> ModelLabFillSourceTicketsResult:
    handler, request_type, _ = OPERATIONS["collect_lab_fill_source_tickets"]
    result = handler().handle(
        request_type.model_validate(
            {"plan_priorities": PRIORITIES, "plan_action_kinds": KINDS, **over}
        )
    )
    assert isinstance(result, ModelLabFillSourceTicketsResult)
    return result


def test_contract_binds_both_source_operations_to_the_plan_command() -> None:
    assert set(OPERATIONS) == set(SOURCE_OPERATIONS)
    entries = {e["operation"]: e for e in _contract()["handler_routing"]["handlers"]}
    for operation in SOURCE_OPERATIONS:
        assert (
            entries[operation]["topic"]
            == "onex.cmd.omnimarket.lab-fill-plan-requested.v1"
        )
    assert OPERATIONS["collect_lab_fill_source_tickets"] == (
        HandlerLabFillSourceTickets,
        ModelLabFillSourceTicketsRequest,
        ModelLabFillSourceTicketsResult,
    )
    assert OPERATIONS["classify_lab_fill_source_candidates"] == (
        HandlerLabFillSourceCandidates,
        ModelLabFillSourceCandidatesRequest,
        ModelLabFillSourceCandidatesResult,
    )


def test_golden_chain_turns_both_sources_into_candidates() -> None:
    """A stalled plan and a pending ruling each become one candidate; the rest drop."""
    sources = _tickets(
        plans={
            "document": {
                "plans": [
                    _plan("OMN-11"),
                    _plan("OMN-12", priority="medium"),
                    _plan("OMN-13", next_action_kind="wait"),
                    _plan("OMN-14", verdict="MOVING"),
                ]
            }
        },
        rulings={
            "document": {
                "unmechanized": [
                    {
                        "ts": "2026-10-09T10:00:00Z",
                        "lane": "relay",
                        "mechanism_tickets": ["OMN-21", "OMN-22"],
                    },
                    {
                        "ts": "2026-10-09T11:00:00Z",
                        "lane": "relay",
                        "mechanism_tickets": [],
                    },
                ]
            }
        },
    )
    assert sources.wanted == ("OMN-11", "OMN-21", "OMN-22")
    assert sources.sources["plan-stalled"].read == "ok"
    assert sources.sources["plan-stalled"].reasons == {
        "not-priority": 1,
        "not-lane-work": 1,
    }
    assert sources.sources["ruling-pending"].reasons == {"no-pending-ticket": 1}

    handler, request_type, result_type = OPERATIONS[
        "classify_lab_fill_source_candidates"
    ]
    result = handler().handle(
        request_type(
            sources=sources,
            tickets=(
                _facts(
                    "OMN-11",
                    attachments=(
                        {
                            "url": "https://github.com/OmniNode-ai/omnimarket/pull/7",
                            "created_at": "2026-10-01T00:00:00Z",
                        },
                        {
                            "url": "https://github.com/OmniNode-ai/omnimarket/pull/9",
                            "created_at": "2026-10-02T00:00:00Z",
                        },
                    ),
                    declared_repo="omnimarket",
                ),
                _facts("OMN-21", state_type="completed"),
                _facts("OMN-22"),
            ),
            open_prs=(
                {
                    "key": "omnimarket#40",
                    "repo": "omnimarket",
                    "state": "OPEN",
                    "title": "feat(OMN-22): work",
                },
            ),
        )
    )
    assert isinstance(result, result_type)
    assert [(c.kind, c.ticket) for c in result.candidates] == [
        ("plan-stalled", "OMN-11")
    ]
    only = result.candidates[0]
    assert only.ticket_prs == ("omnimarket#9", "omnimarket#7")
    assert only.declared_repo == "omnimarket"
    assert only.evidence == "plan 2026-10-01-lab.md, priority high, idle 30 h"
    assert result.sources["ruling-pending"].reasons == {
        "no-pending-ticket": 1,
        "done": 1,
        "open-pr": 1,
    }
    assert result.sources["ruling-pending"].dropped == 3


@pytest.mark.parametrize(
    ("over", "kind", "read"),
    [
        (
            {"plans": {"document": {"other": []}}},
            "plan-stalled",
            "unreadable:plans-today printed no plans list",
        ),
        (
            {"plans": {"document": None}},
            "plan-stalled",
            "unreadable:plans-today printed no plans list",
        ),
        (
            {"rulings": {"document": {"unmechanized": "x"}}},
            "ruling-pending",
            "unreadable:ruling-mechanisms printed no unmechanized list",
        ),
        (
            {"rulings": {"document": {"unmechanized": [{"mechanism_tickets": 3}]}}},
            "ruling-pending",
            "unreadable:'int' object is not iterable",
        ),
        (
            {"plans": {"error": "plans-today exit 2  Traceback\n  boom"}},
            "plan-stalled",
            "unreadable:plans-today exit 2 Traceback boom",
        ),
        ({}, "plan-stalled", "empty"),
    ],
)
def test_error_chain_names_an_unreadable_source(
    over: dict[str, Any], kind: str, read: str
) -> None:
    result = _tickets(**over)
    assert result.sources[kind].read == read
    assert not [f for f in result.found if f.kind == kind]


def test_error_chain_a_failed_ticket_read_names_every_read_source() -> None:
    sources = _tickets(
        plans={"document": {"plans": []}},
        rulings={"document": {"unmechanized": [{"mechanism_tickets": ["OMN-5"]}]}},
    )
    handler, request_type, _ = OPERATIONS["classify_lab_fill_source_candidates"]
    result = handler().handle(
        request_type(
            sources=sources,
            tickets=(_facts("OMN-5"),),
            read_error="unreadable-from-snapshot: watcher freshness/schema",
        )
    )
    assert result.candidates == ()
    assert {k: d.read for k, d in result.sources.items()} == {
        "plan-stalled": "unreadable:unreadable-from-snapshot: watcher freshness/schema",
        "ruling-pending": "unreadable:unreadable-from-snapshot: watcher freshness/schema",
    }


def test_error_chain_a_ticket_linear_did_not_return_is_dropped() -> None:
    sources = _tickets(plans={"document": {"plans": [_plan("OMN-8")]}})
    handler, request_type, _ = OPERATIONS["classify_lab_fill_source_candidates"]
    result = handler().handle(request_type(sources=sources))
    assert result.candidates == ()
    assert result.sources["plan-stalled"].reasons == {"ticket-unreadable": 1}


@pytest.mark.parametrize(
    "payload",
    [
        {"plan_action_kinds": KINDS},
        {"plan_priorities": PRIORITIES, "plan_action_kinds": KINDS, "extra": 1},
        {
            "plan_priorities": PRIORITIES,
            "plan_action_kinds": KINDS,
            "plans": {"doc": {}},
        },
    ],
)
def test_error_chain_refuses_a_malformed_request(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelLabFillSourceTicketsRequest.model_validate(payload)


def test_fixture_covers_both_operations_and_every_drop_reason() -> None:
    assert len({c["name"] for c in CASES}) == len(CASES)
    assert all(sum(c["op"] == op for c in CASES) >= 20 for op in SOURCE_OPERATIONS)
    reasons = {
        reason
        for c in CASES
        for diagnostic in c["expected"]["sources"].values()
        for reason in diagnostic["reasons"]
    }
    assert reasons == {
        "not-priority",
        "not-lane-work",
        "no-ticket",
        "no-pending-ticket",
        "ticket-unreadable",
        "done",
        "has-children",
        "open-pr",
    }
    assert sum(len(c["expected"].get("candidates", [])) for c in CASES) >= 20


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_sources_match_the_workflow(case: dict[str, Any]) -> None:
    handler_type, request_type, result_type = OPERATIONS[case["op"]]
    result = handler_type().handle(request_type.model_validate(case["input"]))
    assert isinstance(result, result_type)
    assert result.model_dump(mode="json") == case["expected"]
