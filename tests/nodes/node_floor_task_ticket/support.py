# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two floor-task ticket nodes composed on one stubbed Linear: payload compute, then the mint effect."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from omnibase_spi.contracts.services import (
    ModelFloorTicketCreateRequest,
    ModelFloorTicketCreateResult,
)

from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
)
from omnimarket.nodes.node_create_ticket.handlers.core_ticket_guard import (
    CoreTicketGuard,
    core_lane_intake_policy,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    TicketGuardRefusedError,
)
from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_graphql_floor_ticket_store import (
    READ_QUERY,
    SEARCH_QUERY,
    GraphqlFloorTicketStore,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers import (
    HandlerFloorTaskTicketMint,
)
from omnimarket.nodes.node_floor_task_ticket_effect.models import (
    ModelFloorTaskTicketMintRequest,
    ModelFloorTaskTicketMintResult,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.handlers.handler_floor_task_ticket_payload import (
    HandlerFloorTaskTicketPayload,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.models import (
    ModelFloorTaskTicketPayloadRequest,
    ModelFloorTaskTicketPayloadResult,
)

LANE = "t11-gate-test"
TEAM = "Omninode"
DEADLINE = 5.0


class RecordingLinearClient:
    """``node_create_ticket``'s Linear client, stubbed: it records each create and keeps the issue for the reader."""

    def __init__(
        self, issues: dict[str, dict[str, Any]], first_number: int = 91001
    ) -> None:
        self.issues = issues
        self.calls: list[dict[str, object]] = []
        self._next = first_number

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
        self.calls.append(
            {
                "title": title,
                "description": description,
                "team": team,
                "parent": parent,
                "assignee_id": assignee_id,
                "label_names": list(label_names),
            }
        )
        identifier = f"OMN-{self._next}"
        self._next += 1
        self.issues[identifier] = {
            "identifier": identifier,
            "state": {"name": "Backlog"},
            "description": description,
            "project": None,
            "labels": {"nodes": [{"name": name} for name in label_names]},
            "children": {"nodes": []},
        }
        return identifier, f"https://linear.invalid/issue/{identifier}"


class FakeGraphql:
    """Linear's GraphQL as the store asks it, answered from the issues the stubbed client created."""

    def __init__(self, issues: dict[str, dict[str, Any]]) -> None:
        self.issues = issues
        self.posts: list[str] = []

    def post(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        if query == READ_QUERY:
            self.posts.append("read")
            return {"data": {"issue": self.issues.get(str(variables["id"]))}}
        assert query == SEARCH_QUERY
        self.posts.append("search")
        needle = str(variables["needle"])
        return {
            "data": {
                "issues": {
                    "nodes": [
                        {"identifier": key}
                        for key, issue in self.issues.items()
                        if needle in (issue["description"] or "")
                    ]
                }
            }
        }


def creator_over_create_ticket(
    handler: HandlerCreateTicket,
) -> Callable[[ModelFloorTicketCreateRequest], ModelFloorTicketCreateResult]:
    """The binding of the store's create to ``node_create_ticket``: the gate runs inside the handler, once."""

    def create(request: ModelFloorTicketCreateRequest) -> ModelFloorTicketCreateResult:
        try:
            created = handler.handle(
                ModelCreateTicketRequest(
                    operation=EnumTicketOperation.CREATE,
                    title=request.title,
                    description=request.description,
                    parent=request.parent,
                    team=TEAM,
                )
            )
        except TicketGuardRefusedError as refused:
            return ModelFloorTicketCreateResult(
                state="refused", cause=",".join(refused.finding_codes) or "gate-refused"
            )
        return ModelFloorTicketCreateResult(state="created", ticket=created.ticket_id)

    return create


class StubbedCreateTicket:
    """A real ``HandlerCreateTicket`` (lane intake and the guard it is given) over a stubbed Linear client."""

    def __init__(
        self, handler_guard: Any, intake: Any, first_number: int = 91001
    ) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.client = RecordingLinearClient(self.issues, first_number)
        self.graphql = FakeGraphql(self.issues)
        handler = HandlerCreateTicket(
            linear_client=self.client,
            ticket_guard=handler_guard,
            lane_intake_policy=intake,
            lane_id=LANE,
        )
        self.store = GraphqlFloorTicketStore(
            graphql=self.graphql, creator=creator_over_create_ticket(handler)
        )


def real_gate_create_ticket() -> StubbedCreateTicket:
    """``node_create_ticket`` with its own gate, the omnibase_core validator run in process, and a stubbed Linear."""
    return StubbedCreateTicket(CoreTicketGuard(LANE), core_lane_intake_policy())


def payload_request(
    line_id: str, task_id: str, plan_text: str | None
) -> ModelFloorTaskTicketPayloadRequest:
    return ModelFloorTaskTicketPayloadRequest(
        line_id=line_id, task_id=task_id, plan_text=plan_text
    )


def mint_request_of(
    payload_result: ModelFloorTaskTicketPayloadResult, timeout: float = DEADLINE
) -> ModelFloorTaskTicketMintRequest:
    assert payload_result.payload is not None
    return ModelFloorTaskTicketMintRequest(
        **payload_result.payload.model_dump(), mint_timeout_seconds=timeout
    )


def mint_task(
    store: Any,
    line_id: str,
    task_id: str,
    plan_text: str | None,
    timeout: float = DEADLINE,
) -> tuple[ModelFloorTaskTicketPayloadResult, ModelFloorTaskTicketMintResult | None]:
    """The orchestrator's pass for one task: the payload verdict first, the mint only when a payload exists."""
    verdict = HandlerFloorTaskTicketPayload().handle(
        payload_request(line_id, task_id, plan_text)
    )
    if verdict.payload is None:
        return verdict, None
    return verdict, HandlerFloorTaskTicketMint(store).handle(
        mint_request_of(verdict, timeout)
    )
