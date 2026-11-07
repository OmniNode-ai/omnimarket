# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend ticket store: read and search over GraphQL, create through the injected creator (answered from a stub)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from omnibase_spi.contracts.services import (
    ModelFloorTicketCreateRequest,
    ModelFloorTicketCreateResult,
    ModelFloorTicketLookupRequest,
    ModelFloorTicketSearchRequest,
)
from omnibase_spi.protocols.lane import ProtocolFloorTicketStore

from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_graphql_floor_ticket_store import (
    READ_QUERY,
    SEARCH_QUERY,
    GraphqlFloorTicketStore,
)

pytestmark = pytest.mark.unit

ISSUE = {
    "identifier": "OMN-1",
    "state": {"name": "Backlog"},
    "description": "Floor key: floor:a:T1\n",
    "project": {"name": "Sprint"},
    "labels": {"nodes": [{"name": "lane-created"}, {"name": "delegation"}]},
    "children": {"nodes": [{"id": "child"}]},
}


class Graphql:
    def __init__(
        self, data: Mapping[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.data = data or {}
        self.error = error
        self.posts: list[tuple[str, Mapping[str, object]]] = []

    def post(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        self.posts.append((query, variables))
        if self.error is not None:
            raise self.error
        return self.data


def created(request: ModelFloorTicketCreateRequest) -> ModelFloorTicketCreateResult:
    return ModelFloorTicketCreateResult(state="created", ticket="OMN-9")


def store(graphql: Graphql, creator: Any = created) -> GraphqlFloorTicketStore:
    return GraphqlFloorTicketStore(graphql=graphql, creator=creator)


def test_read_maps_the_issue_to_the_facts_the_mint_judges_by() -> None:
    graphql = Graphql({"data": {"issue": ISSUE}})
    found = store(graphql).read(ModelFloorTicketLookupRequest(ticket="OMN-1"))
    assert (found.state, found.ticket, found.status) == ("found", "OMN-1", "Backlog")
    assert found.description == "Floor key: floor:a:T1\n"
    assert found.project == "Sprint"
    assert found.labels == ("lane-created", "delegation")
    assert found.has_children is True
    assert graphql.posts == [(READ_QUERY, {"id": "OMN-1"})]


def test_read_of_an_issue_with_no_project_and_no_children() -> None:
    bare = {
        **ISSUE,
        "project": None,
        "children": {"nodes": []},
        "labels": {"nodes": []},
    }
    found = store(Graphql({"data": {"issue": bare}})).read(
        ModelFloorTicketLookupRequest(ticket="OMN-1")
    )
    assert (found.project, found.labels, found.has_children) == (None, (), False)


def test_read_of_no_issue_is_missing_and_a_failing_read_is_failed_naming_only_the_type() -> (
    None
):
    assert (
        store(Graphql({"data": {"issue": None}}))
        .read(ModelFloorTicketLookupRequest(ticket="OMN-1"))
        .state
        == "missing"
    )
    failed = store(Graphql(error=RuntimeError("token abc"))).read(
        ModelFloorTicketLookupRequest(ticket="OMN-1")
    )
    assert (failed.state, failed.cause) == ("failed", "RuntimeError")


def test_search_asks_for_the_exact_floor_key_line_and_returns_the_ids() -> None:
    graphql = Graphql(
        {
            "data": {
                "issues": {"nodes": [{"identifier": "OMN-3"}, {"identifier": "OMN-2"}]}
            }
        }
    )
    found = store(graphql).search_by_floor_key(
        ModelFloorTicketSearchRequest(floor_key="floor:a:T1")
    )
    assert (found.state, found.tickets) == ("answered", ("OMN-3", "OMN-2"))
    [(query, variables)] = graphql.posts
    assert query == SEARCH_QUERY
    assert variables["needle"] == "Floor key: floor:a:T1"


def test_a_failing_search_is_failed() -> None:
    failed = store(Graphql(error=OSError("down"))).search_by_floor_key(
        ModelFloorTicketSearchRequest(floor_key="floor:a:T1")
    )
    assert (failed.state, failed.cause) == ("failed", "OSError")


def test_create_is_the_creators_answer_and_a_raising_creator_is_failed() -> None:
    request = ModelFloorTicketCreateRequest(
        floor_key="floor:a:T1", title="t", description="d", parent="OMN-1"
    )
    assert store(Graphql()).create(request).ticket == "OMN-9"

    def raising(_: ModelFloorTicketCreateRequest) -> ModelFloorTicketCreateResult:
        raise RuntimeError("token abc")

    failed = store(Graphql(), raising).create(request)
    assert (failed.state, failed.cause) == ("failed", "RuntimeError")


def test_the_store_satisfies_the_spi_protocol() -> None:
    assert isinstance(store(Graphql()), ProtocolFloorTicketStore)
