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
    ProtocolLinearGraphql,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers.ticket_judge import judge

pytestmark = pytest.mark.unit

ISSUE = {
    "identifier": "OMN-1",
    "state": {"name": "Backlog", "type": "backlog"},
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


def store(
    graphql: ProtocolLinearGraphql, creator: Any = created
) -> GraphqlFloorTicketStore:
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
    graphql = Graphql(page(["OMN-3", "OMN-2"]))
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


# ------------------------------------------------------------------ errors, partial answers, terminal types, pages


def page(
    identifiers: list[str], next_cursor: str | None = None, has_next: bool = False
) -> dict[str, Any]:
    return {
        "data": {
            "issues": {
                "nodes": [{"identifier": identifier} for identifier in identifiers],
                "pageInfo": {"hasNextPage": has_next, "endCursor": next_cursor},
            }
        }
    }


class Pages:
    """A GraphQL double that answers the search one scripted page at a time."""

    def __init__(self, *pages: Mapping[str, Any]) -> None:
        self.pages = list(pages)
        self.posts: list[Mapping[str, object]] = []

    def post(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        self.posts.append(variables)
        return self.pages[len(self.posts) - 1]


SEARCH = ModelFloorTicketSearchRequest(floor_key="floor:a:T1")


@pytest.mark.parametrize(
    "answer",
    [
        {"data": None, "errors": [{"message": "unauthorized"}]},
        {"data": {"issues": None}},
        {"data": {"issues": {"nodes": None}}},
        {"data": {"issues": {"nodes": []}}},
        {**page(["OMN-1"]), "errors": [{"message": "partial"}]},
        {},
    ],
)
def test_search_error_or_incomplete_answers_are_failed_never_an_empty_answer(
    answer: Mapping[str, Any],
) -> None:
    result = store(Graphql(answer)).search_by_floor_key(SEARCH)
    assert (result.state, result.tickets) == ("failed", ())


def test_search_of_an_empty_connection_is_answered_with_no_tickets() -> None:
    result = store(Graphql(page([]))).search_by_floor_key(SEARCH)
    assert (result.state, result.tickets) == ("answered", ())


def test_search_follows_every_page_so_a_match_past_the_first_page_is_found() -> None:
    graphql = Pages(
        page(["OMN-10"], next_cursor="c1", has_next=True),
        page(["OMN-11", "OMN-2"]),
    )
    result = store(graphql).search_by_floor_key(SEARCH)
    assert (result.state, result.tickets) == ("answered", ("OMN-10", "OMN-11", "OMN-2"))
    assert [post.get("after") for post in graphql.posts] == [None, "c1"]


def test_search_whose_next_page_cannot_be_reached_is_failed() -> None:
    no_cursor = Pages(page(["OMN-10"], next_cursor=None, has_next=True))
    assert store(no_cursor).search_by_floor_key(SEARCH).state == "failed"
    stuck = Pages(
        page(["OMN-10"], next_cursor="c1", has_next=True),
        page(["OMN-11"], next_cursor="c1", has_next=True),
    )
    assert store(stuck).search_by_floor_key(SEARCH).state == "failed"


@pytest.mark.parametrize(
    "answer",
    [
        {"data": None, "errors": [{"message": "unauthorized"}]},
        {"data": {"issue": ISSUE}, "errors": [{"message": "project resolver"}]},
        {},
        {"data": {}},
        {"data": {"issue": {k: v for k, v in ISSUE.items() if k != "project"}}},
        {"data": {"issue": {k: v for k, v in ISSUE.items() if k != "labels"}}},
        {"data": {"issue": {k: v for k, v in ISSUE.items() if k != "children"}}},
        {"data": {"issue": {k: v for k, v in ISSUE.items() if k != "state"}}},
        {"data": {"issue": {k: v for k, v in ISSUE.items() if k != "description"}}},
    ],
)
def test_read_error_or_incomplete_answers_are_failed_never_safe_looking_facts(
    answer: Mapping[str, Any],
) -> None:
    found = store(Graphql(answer)).read(ModelFloorTicketLookupRequest(ticket="OMN-1"))
    assert found.state == "failed"


@pytest.mark.parametrize(
    ("name", "kind", "verdict"),
    [
        ("Released", "completed", "done"),
        ("Shelved", "canceled", "canceled"),
        ("Released", "started", "usable"),
    ],
)
def test_a_custom_named_terminal_state_is_judged_by_its_state_type(
    name: str, kind: str, verdict: str
) -> None:
    open_issue = {
        **ISSUE,
        "state": {"name": name, "type": kind},
        "project": None,
        "labels": {"nodes": []},
        "children": {"nodes": []},
    }
    found = store(Graphql({"data": {"issue": open_issue}})).read(
        ModelFloorTicketLookupRequest(ticket="OMN-1")
    )
    assert found.state == "found"
    assert judge(found, "floor:a:T1") == verdict
