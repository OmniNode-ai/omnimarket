# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus ticket path holds a lab lane to the create gate's duplicate search and reads a parent.

A lab lane has no Linear tool, so the node runs next to the key. Two things it must do for that lane:
refuse a create whose title an open ticket of the team already carries (the skill's duplicate search),
and answer a read with the ticket's live parent, so the remote-lane runner proves a child claim without
holding the key. Golden chain: a create with no duplicate is filed and a read names the parent. Error
chain: a duplicate is a typed ``guard_refused`` terminal with ``duplicate_ticket`` and the existing id,
nothing is filed, and a search that cannot answer files nothing either. The failing control for each is
the same request with the search answering the other way.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnimarket.nodes.node_create_ticket.handlers import handler_create_ticket
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    EnumTicketOperation,
    HandlerCreateTicket,
    ModelCreateTicketRequest,
    ModelTicketGuardDecision,
    TicketLookupProtocol,
)
from omnimarket.nodes.node_linear_ticket_write_effect.handlers.handler_linear_ticket_write import (
    HandlerLinearTicketWriteEffect,
)
from omnimarket.nodes.node_linear_ticket_write_effect.models.model_ticket_write_io import (
    ModelTicketWriteRequest,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("lane_create_context")]


class _Guard:
    def check(self, tool_input: dict[str, object]) -> ModelTicketGuardDecision:
        del tool_input
        return ModelTicketGuardDecision(admitted=True, reason="", guard_path="/g.py")


class _Linear:
    """A fake Linear gateway: records creates, answers the two lookups from fields."""

    def __init__(
        self, duplicates: list[str] | None = None, search_error: bool = False
    ) -> None:
        self.created = 0
        self.duplicates = duplicates or []
        self.search_error = search_error

    def create_ticket(
        self,
        *,
        title: str,
        description: str,
        team: str,
        parent: str | None,
        assignee_id: str | None,
        label_names: Sequence[str] = (),
    ) -> tuple[str, str]:
        self.created += 1
        return "OMN-91001", "https://linear.app/omninode/issue/OMN-91001"

    def read_parent(self, ticket_id: str) -> str:
        return "OMN-22478" if ticket_id == "OMN-91001" else ""

    def find_open_duplicates(self, *, team: str, title: str) -> list[str]:
        if self.search_error:
            raise RuntimeError("Linear answered 500 with a header carrying a secret")
        return list(self.duplicates)


class _Tracker:
    async def get_issue(self, ticket_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            identifier=ticket_id,
            url="u",
            title="child",
            state="Backlog",
            description="d",
        )

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds


def _effect(linear: _Linear) -> HandlerLinearTicketWriteEffect:
    return HandlerLinearTicketWriteEffect(
        handler_factory=lambda _lane: HandlerCreateTicket(
            linear_client=linear, tracker=_Tracker(), ticket_guard=_Guard()
        )
    )


def _command(**fields: object) -> ModelTicketWriteRequest:
    return ModelTicketWriteRequest.model_validate(
        {
            "operation": "create",
            "title": "a fixture child",
            "description": "created-by-lane: lane-a\n\nGate: OMN-17427 AC-1",
            "parent": "OMN-22478",
            "correlation_id": str(uuid4()),
            "requested_by_lane": "lane-a",
            "idempotency_key": str(uuid4()),
            **fields,
        }
    )


def test_the_fake_and_the_real_gateway_both_satisfy_the_lookup_protocol() -> None:
    assert isinstance(_Linear(), TicketLookupProtocol)
    assert isinstance(
        handler_create_ticket.LinearTicketHttpGateway("k"), TicketLookupProtocol
    )


async def test_golden_a_create_with_no_duplicate_is_filed() -> None:
    linear = _Linear()
    result = await _effect(linear).handle(_command())
    assert result.outcome == "completed"
    assert result.result.status == "created"
    assert result.result.ticket_id == "OMN-91001"
    assert linear.created == 1


async def test_error_an_open_ticket_with_the_title_is_a_typed_refusal() -> None:
    linear = _Linear(duplicates=["OMN-77001"])
    result = await _effect(linear).handle(_command())
    assert result.outcome == "failed"
    assert result.reason == "guard_refused"
    assert result.finding_codes == ["duplicate_ticket"]
    assert result.existing_ticket_ids == ["OMN-77001"]
    assert linear.created == 0


async def test_error_a_search_that_cannot_answer_files_nothing_and_leaks_nothing() -> (
    None
):
    linear = _Linear(search_error=True)
    result = await _effect(linear).handle(_command())
    assert result.outcome == "failed"
    assert result.reason == "operation_failed"
    assert "secret" not in result.model_dump_json()
    assert linear.created == 0


async def test_control_the_refusal_is_the_search_not_the_request() -> None:
    # The same request files when the search finds nothing, and is refused when it finds one.
    assert (await _effect(_Linear()).handle(_command())).outcome == "completed"
    refused = await _effect(_Linear(duplicates=["OMN-77001"])).handle(_command())
    assert refused.outcome == "failed"


def test_a_dry_run_calls_no_linear_lookup() -> None:
    linear = _Linear(duplicates=["OMN-77001"])
    result = HandlerCreateTicket(linear_client=linear, ticket_guard=_Guard()).handle(
        ModelCreateTicketRequest(
            title="t",
            description="created-by-lane: lane-a\n\nGate: OMN-17427 AC-1",
            dry_run=True,
        )
    )
    assert result.status == "dry_run"
    assert linear.created == 0


def test_golden_a_read_names_the_live_parent() -> None:
    result = HandlerCreateTicket(linear_client=_Linear(), tracker=_Tracker()).handle(
        ModelCreateTicketRequest(
            operation=EnumTicketOperation.READ, ticket_id="OMN-91001"
        )
    )
    assert result.parent_ticket_id == "OMN-22478"


def test_a_read_of_a_ticket_without_a_parent_says_empty_and_not_unread() -> None:
    result = HandlerCreateTicket(linear_client=_Linear(), tracker=_Tracker()).handle(
        ModelCreateTicketRequest(
            operation=EnumTicketOperation.READ, ticket_id="OMN-90000"
        )
    )
    assert result.parent_ticket_id == ""


def test_a_read_with_no_live_gateway_leaves_the_parent_unread() -> None:
    result = HandlerCreateTicket(tracker=_Tracker()).handle(
        ModelCreateTicketRequest(
            operation=EnumTicketOperation.READ, ticket_id="OMN-91001"
        )
    )
    assert result.parent_ticket_id is None


def _gateway(answer: dict[str, object], seen: list[tuple[str, dict[str, object]]]):
    gateway = handler_create_ticket.LinearTicketHttpGateway("k")

    def post(query: str, variables: dict[str, object]) -> dict[str, object]:
        seen.append((query, variables))
        return answer

    gateway._post = post  # type: ignore[method-assign]
    return gateway


def test_the_real_gateway_searches_open_tickets_of_the_team_by_title() -> None:
    seen: list[tuple[str, dict[str, object]]] = []
    gateway = _gateway(
        {"data": {"issues": {"nodes": [{"identifier": "OMN-77001"}]}}}, seen
    )
    assert gateway.find_open_duplicates(team="Omninode", title="T") == ["OMN-77001"]
    query, variables = seen[0]
    assert "eqIgnoreCase" in query
    assert '"canceled"' in query
    assert '"completed"' in query
    assert variables == {"team": "Omninode", "title": "T"}


def test_the_real_gateway_never_reads_a_failed_search_as_no_duplicate() -> None:
    gateway = _gateway({"errors": [{"message": "x"}]}, [])
    with pytest.raises(RuntimeError):
        gateway.find_open_duplicates(team="Omninode", title="T")


def test_the_real_gateway_reads_a_parent_and_an_absent_parent() -> None:
    with_parent = _gateway({"data": {"issue": {"parent": {"identifier": "OMN-1"}}}}, [])
    assert with_parent.read_parent("OMN-2") == "OMN-1"
    without = _gateway({"data": {"issue": {"parent": None}}}, [])
    assert without.read_parent("OMN-2") == ""
    missing = _gateway({"data": {"issue": None}}, [])
    with pytest.raises(RuntimeError):
        missing.read_parent("OMN-3")
