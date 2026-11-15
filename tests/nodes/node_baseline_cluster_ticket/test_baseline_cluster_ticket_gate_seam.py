# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the cluster ticket node and the ticket node's creation gate (OMN-19998).

The node writes the description a created ticket carries; ``node_create_ticket`` admits or refuses it with the omnibase_core
creation gate. This test runs the real gate (a stubbed Linear behind it), so it fails when the node's description stops
satisfying the gate or the gate asks for a line the node does not write. The two controls remove a line the gate requires.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.adapters.adapter_graphql_cluster_ticket_store import (
    COMMENT_MUTATION,
    READ_QUERY,
    SEARCH_QUERY,
    GraphqlClusterTicketStore,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers import (
    HandlerBaselineClusterTicket,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.models import (
    ModelBaselineClusterReportRequest,
    ModelClusterTicketCreateRequest,
    ModelClusterTicketCreateResult,
)
from omnimarket.nodes.node_create_ticket.handlers.core_ticket_guard import (
    CoreTicketGuard,
    core_lane_intake_policy,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    TicketGuardRefusedError,
)
from tests.nodes.node_floor_task_ticket.support import LANE, RecordingLinearClient

pytestmark = pytest.mark.unit


def report() -> ModelBaselineClusterReportRequest:
    return ModelBaselineClusterReportRequest.model_validate(
        {
            "repo": "omnibase_infra",
            "cluster": "tests.unit.test_foo",
            "failing_count": 2,
            "examples": ("tests.unit.test_foo::test_a",),
            "run_url": "https://ci.example.invalid/run/1",
            "observed_at": "2026-11-15T03:00:00+00:00",
            "requested_by_lane": LANE,
            "parent": "OMN-22847",
            "gate": "example-org/example-repo example-workflow.yml",
            "diagnosis": "example-org/example-repo#1",
            "timeout_seconds": 5.0,
            "max_duplicate_hops": 3,
            "example_limit": 5,
        }
    )


class FakeLinearGraphql:
    """Linear's GraphQL as the cluster store asks it, answered from the issues the stubbed ticket client created."""

    def __init__(self, issues: dict[str, dict[str, Any]]) -> None:
        self.issues = issues
        self.comments: list[tuple[str, str]] = []

    def post(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        if query == READ_QUERY:
            issue = self.issues.get(str(variables["id"]))
            if issue is None:
                return {"data": {"issue": None}}
            return {
                "data": {
                    "issue": {
                        "id": f"uuid-{issue['identifier']}",
                        "identifier": issue["identifier"],
                        "state": issue["state"],
                        "description": issue["description"],
                        "relations": {"nodes": []},
                    }
                }
            }
        if query == SEARCH_QUERY:
            needle = str(variables["needle"])
            return {
                "data": {
                    "issues": {
                        "nodes": [
                            {"identifier": key}
                            for key, issue in self.issues.items()
                            if needle in (issue["description"] or "")
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        assert query == COMMENT_MUTATION
        self.comments.append((str(variables["issueId"]), str(variables["body"])))
        return {
            "data": {
                "commentCreate": {
                    "success": True,
                    "comment": {"id": f"comment-{len(self.comments)}"},
                }
            }
        }


def creator_over(
    handler: HandlerCreateTicket,
    rewrite: Callable[[str], str] = lambda description: description,
) -> Callable[[ModelClusterTicketCreateRequest], ModelClusterTicketCreateResult]:
    def create(
        request: ModelClusterTicketCreateRequest,
    ) -> ModelClusterTicketCreateResult:
        try:
            created = handler.handle(
                ModelCreateTicketRequest(
                    operation=EnumTicketOperation.CREATE,
                    title=request.title,
                    description=rewrite(request.description),
                    parent=request.parent,
                )
            )
        except TicketGuardRefusedError as refused:
            return ModelClusterTicketCreateResult(
                state="refused", cause=",".join(refused.finding_codes) or "gate-refused"
            )
        return ModelClusterTicketCreateResult(state="created", ticket=created.ticket_id)

    return create


def composed(
    rewrite: Callable[[str], str] = lambda description: description,
) -> tuple[HandlerBaselineClusterTicket, RecordingLinearClient, FakeLinearGraphql]:
    """The cluster handler over the GraphQL store, its create over the real gate and a stubbed Linear client."""
    issues: dict[str, dict[str, Any]] = {}
    client = RecordingLinearClient(issues)
    create_ticket = HandlerCreateTicket(
        linear_client=client,
        ticket_guard=CoreTicketGuard(LANE),
        lane_intake_policy=core_lane_intake_policy(),
        lane_id=LANE,
    )
    graphql = FakeLinearGraphql(issues)
    store = GraphqlClusterTicketStore(
        graphql=graphql, creator=creator_over(create_ticket, rewrite)
    )
    return HandlerBaselineClusterTicket(store), client, graphql


def test_the_description_the_node_writes_passes_the_real_creation_gate() -> None:
    handler, client, _ = composed()
    result = handler.handle(report())
    assert result.state == "created"
    assert result.ticket is not None
    (call,) = client.calls
    assert call["parent"] == "OMN-22847"
    assert "Cluster signature: dev-baseline:omnibase_infra:tests.unit.test_foo" in str(
        call["description"]
    )


def test_two_runs_of_the_same_cluster_are_one_create_and_one_comment_through_the_real_gate() -> (
    None
):
    handler, client, graphql = composed()
    first = handler.handle(report())
    second = handler.handle(report())
    assert first.state == "created"
    assert second.state == "commented"
    assert second.ticket == first.ticket
    assert len(client.calls) == 1
    assert len(graphql.comments) == 1
    assert graphql.comments[0][1].startswith("Still failing: ")


def test_control_a_description_without_the_diagnosis_line_is_refused_by_the_real_gate() -> (
    None
):
    handler, client, _ = composed(
        rewrite=lambda text: "\n".join(
            line for line in text.splitlines() if not line.startswith("Diagnosis:")
        )
    )
    result = handler.handle(report())
    assert result.state == "refused"
    assert result.cause is not None
    assert "missing_diagnosis" in result.cause
    assert client.calls == []


def test_control_a_description_without_the_gate_line_is_refused_by_the_real_gate() -> (
    None
):
    handler, client, _ = composed(
        rewrite=lambda text: "\n".join(
            line for line in text.splitlines() if not line.startswith("Gate:")
        )
    )
    result = handler.handle(report())
    assert result.state == "refused"
    assert client.calls == []
