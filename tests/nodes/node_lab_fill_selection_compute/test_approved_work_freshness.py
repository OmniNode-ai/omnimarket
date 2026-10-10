# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Approved-work completion comes from supplied facts (OMN-17427)."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_selection_compute.handlers import (
    HandlerApprovedWorkFreshness,
)
from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    ModelApprovedWorkEvidence,
    ModelApprovedWorkFreshnessRequest,
    ModelApprovedWorkTicketFacts,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "approved_work_freshness.json"


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _request() -> ModelApprovedWorkFreshnessRequest:
    data = _fixture()
    return ModelApprovedWorkFreshnessRequest(
        rows=tuple(data["rows"]),
        facts=tuple(
            ModelApprovedWorkTicketFacts(
                **{
                    **fact,
                    "evidence": tuple(
                        ModelApprovedWorkEvidence(**e) for e in fact["evidence"]
                    ),
                }
            )
            for fact in data["facts"]
        ),
        checked_at=data["checked_at"],
    )


def test_fixture_completion_and_remaining_match_facts() -> None:
    data, request = _fixture(), _request()
    result = HandlerApprovedWorkFreshness().handle(request)
    rows = {row["ticket"]: row for row in result.rows}
    facts = {fact.ticket: fact for fact in request.facts}
    assert {ticket for ticket, row in rows.items() if row.get("done") is True} == set(
        data["expected_done"]
    )
    for ticket in data["expected_done"]:
        row, fact = rows[ticket], facts[ticket]
        linear = fact.linear_state_type in {"completed", "canceled", "duplicate"}
        assert row["done_basis"] == ("linear-state" if linear else "acceptance-on-main")
        assert row["done_evidence"]
        for evidence in fact.evidence:
            assert f"{evidence.pr}@{evidence.commit[:12]}" in str(row["done_evidence"])
        if linear:
            assert str(row["done_evidence"]).startswith(
                f"linear:{fact.linear_state_name}"
            )
        assert "remaining" not in row
    assert {ticket for ticket, row in rows.items() if row.get("done") is False} == set(
        data["expected_open_with_remaining"]
    )
    for ticket in data["expected_open_with_remaining"]:
        assert rows[ticket]["remaining"] == facts[ticket].remaining
        assert "done_basis" not in rows[ticket]
        assert "done_evidence" not in rows[ticket]
    assert all(verdict.changed for verdict in result.verdicts)
    assert [v.row_id for v in result.verdicts] == [row["id"] for row in request.rows]


@pytest.mark.parametrize("status", ["", "open"])
def test_missing_or_open_facts_leave_rows_byte_identical(status: str) -> None:
    request = _request()
    row = {**request.rows[0], "ticket": "OMN-99999"}
    rows = (row, {**row, "id": "no-facts", "ticket": "OMN-99998"})
    fact = ModelApprovedWorkTicketFacts(
        "OMN-99999", linear_state_type="started", acceptance_status=status
    )
    result = HandlerApprovedWorkFreshness().handle(
        replace(request, rows=rows, facts=(fact,))
    )
    assert json.dumps(result.rows) == json.dumps(rows)
    assert all(
        v.basis == "unchanged" and v.done is None and not v.changed
        for v in result.verdicts
    )
    assert all(
        output is not original
        for output, original in zip(result.rows, rows, strict=True)
    )


@pytest.mark.parametrize("state", ["completed", "canceled", "duplicate"])
def test_linear_completion_precedes_acceptance_and_removes_remaining(
    state: str,
) -> None:
    request = _request()
    fact = replace(
        request.facts[0],
        linear_state_name="Closed",
        linear_state_type=state,
        acceptance_status="partial",
    )
    row = {**request.rows[0], "remaining": "old remainder"}
    result = HandlerApprovedWorkFreshness().handle(
        replace(request, rows=(row,), facts=(fact,))
    )
    assert result.rows[0]["done"] is True
    assert (
        result.rows[0]["done_evidence"]
        == f"linear:Closed; {fact.evidence[0].pr}@{fact.evidence[0].commit[:12]}"
    )
    assert result.verdicts[0].basis == "linear-state"
    assert "remaining" not in result.rows[0]


@pytest.mark.parametrize(
    "fact",
    [
        ModelApprovedWorkTicketFacts("OMN-1", acceptance_status="pass"),
        ModelApprovedWorkTicketFacts("OMN-1", acceptance_status="partial"),
        ModelApprovedWorkTicketFacts(
            "OMN-1", acceptance_status="partial", remaining=" \t"
        ),
        ModelApprovedWorkTicketFacts(
            "OMN-1", acceptance_status="partial", remaining="a\nb"
        ),
        ModelApprovedWorkTicketFacts(
            "OMN-1", acceptance_status="partial", remaining="a\rb"
        ),
        ModelApprovedWorkTicketFacts(
            "OMN-1", acceptance_status="partial", remaining="x" * 301
        ),
        ModelApprovedWorkTicketFacts(
            "OMN-1", evidence=(ModelApprovedWorkEvidence("repo#1", "bad"),)
        ),
        ModelApprovedWorkTicketFacts(
            "OMN-1", evidence=(ModelApprovedWorkEvidence("repo#1", "A" * 40),)
        ),
        ModelApprovedWorkTicketFacts(
            "OMN-1", evidence=(ModelApprovedWorkEvidence("owner/repo#1", "a" * 40),)
        ),
    ],
)
def test_invalid_facts_fail_fast_naming_ticket(
    fact: ModelApprovedWorkTicketFacts,
) -> None:
    request = replace(
        _request(), rows=({"id": "test", "ticket": "OMN-1"},), facts=(fact,)
    )
    with pytest.raises(ValueError, match="OMN-1"):
        HandlerApprovedWorkFreshness().handle(request)


def test_duplicate_facts_are_rejected() -> None:
    request = _request()
    with pytest.raises(ValueError, match=request.facts[0].ticket):
        HandlerApprovedWorkFreshness().handle(
            replace(request, facts=(*request.facts, request.facts[0]))
        )


@pytest.mark.parametrize(
    "checked_at",
    ["bad", "2026-10-07", "2026-10-07T08:40:00", "2026-10-07T08:40:00+01:00"],
)
def test_checked_at_requires_a_utc_timestamp(checked_at: str) -> None:
    with pytest.raises(ValueError, match="checked_at"):
        HandlerApprovedWorkFreshness().handle(
            replace(_request(), checked_at=checked_at)
        )


def test_idempotence_and_inputs_are_not_mutated() -> None:
    request = _request()
    original = deepcopy(request)
    first = HandlerApprovedWorkFreshness().handle(request)
    second = HandlerApprovedWorkFreshness().handle(replace(request, rows=first.rows))
    assert request == original
    assert json.dumps(first.rows) == json.dumps(second.rows)
    assert all(not v.changed for v in second.verdicts)
    assert all(a is not b for a, b in zip(first.rows, request.rows, strict=True))


def test_existing_keys_keep_order_and_new_keys_append_in_declared_order() -> None:
    request = _request()
    row = {
        "id": "ordered",
        "done_evidence": "old",
        "ticket": request.facts[0].ticket,
        "remaining": "old",
    }
    result = HandlerApprovedWorkFreshness().handle(
        replace(request, rows=(row,), facts=(request.facts[0],))
    )
    assert list(result.rows[0]) == [
        "id",
        "done_evidence",
        "ticket",
        "done",
        "done_basis",
    ]
    partial = ModelApprovedWorkTicketFacts(
        request.facts[0].ticket, acceptance_status="partial", remaining="Finish wiring"
    )
    result = HandlerApprovedWorkFreshness().handle(
        replace(request, rows=result.rows, facts=(partial,))
    )
    assert list(result.rows[0]) == ["id", "ticket", "done", "remaining"]
    assert result.verdicts[0].basis == "partial"
    assert result.verdicts[0].remaining == "Finish wiring"
