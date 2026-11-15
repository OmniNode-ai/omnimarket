# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend cluster ticket store: search, read and comment over GraphQL, create through the injected creator (answered from a stub)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from omnimarket.nodes.node_baseline_cluster_ticket_effect.adapters.adapter_graphql_cluster_ticket_store import (
    COMMENT_MUTATION,
    READ_QUERY,
    SEARCH_QUERY,
    GraphqlClusterTicketStore,
    ProtocolLinearGraphql,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.protocol_cluster_ticket_store import (
    ProtocolClusterTicketStore,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_cluster_ticket_store import (
    ModelClusterTicketCommentRequest,
    ModelClusterTicketCreateRequest,
    ModelClusterTicketCreateResult,
    ModelClusterTicketLookupRequest,
    ModelClusterTicketLookupResult,
    ModelClusterTicketSearchRequest,
)

pytestmark = pytest.mark.unit

SIGNATURE = "Cluster signature: dev-baseline:omnibase_infra:tests.nightly"
LEGACY = "**Cluster:** `tests.nightly`"

ISSUE = {
    "id": "issue-omn-1",
    "identifier": "OMN-1",
    "state": {"name": "Backlog", "type": "backlog"},
    "description": f"{SIGNATURE}\n",
    "relations": {"nodes": []},
}


class Graphql:
    """The dict-backed GraphQL double: each post takes the next answer scripted for its document.

    An unscripted document raises, so the store can never read an answer the test did
    not hand it; a scripted ``Exception`` is raised instead of returned.
    """

    def __init__(
        self,
        script: Mapping[str, Sequence[Mapping[str, Any] | Exception]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.script = {key: list(value) for key, value in (script or {}).items()}
        self.error = error
        self.posts: list[tuple[str, Mapping[str, object]]] = []

    def post(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        self.posts.append((query, variables))
        if self.error is not None:
            raise self.error
        answers = self.script.get(query)
        if not answers:
            raise RuntimeError(f"unscripted query: {query.strip().splitlines()[0]}")
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def created(request: ModelClusterTicketCreateRequest) -> ModelClusterTicketCreateResult:
    return ModelClusterTicketCreateResult(state="created", ticket="OMN-9")


def store(
    graphql: ProtocolLinearGraphql, creator: Any = created
) -> GraphqlClusterTicketStore:
    return GraphqlClusterTicketStore(graphql=graphql, creator=creator)


def read_issue(issue: Mapping[str, Any] | None) -> ModelClusterTicketLookupResult:
    return store(Graphql({READ_QUERY: [{"data": {"issue": issue}}]})).read(
        ModelClusterTicketLookupRequest(ticket="OMN-1")
    )


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


# ------------------------------------------------------------------ read


def test_read_maps_the_issue_to_the_facts_the_cluster_report_judges_by() -> None:
    graphql = Graphql({READ_QUERY: [{"data": {"issue": ISSUE}}]})
    found = store(graphql).read(ModelClusterTicketLookupRequest(ticket="OMN-1"))
    assert (found.state, found.ticket, found.status) == ("found", "OMN-1", "open")
    assert found.description == f"{SIGNATURE}\n"
    assert found.duplicate_of is None
    assert graphql.posts == [(READ_QUERY, {"id": "OMN-1"})]


@pytest.mark.parametrize(
    ("state", "verdict"),
    [
        ({"name": "Done", "type": "completed"}, "done"),
        ({"name": "Duplicate", "type": "canceled"}, "duplicate"),
        ({"name": "Won't do", "type": "canceled"}, "canceled"),
        ({"name": "In Progress", "type": "started"}, "open"),
    ],
)
def test_read_judges_the_ticket_by_its_state_type_and_only_a_duplicate_named_one_is_a_duplicate(
    state: dict[str, Any], verdict: str
) -> None:
    found = read_issue({**ISSUE, "state": state})
    assert (found.state, found.ticket, found.status) == ("found", "OMN-1", verdict)
    assert found.duplicate_of is None


def test_a_duplicate_relation_names_the_survivor_of_the_first_one() -> None:
    duplicated = {
        **ISSUE,
        "state": {"name": "Won't do", "type": "canceled"},
        "relations": {
            "nodes": [
                {"type": "blocked", "relatedIssue": {"identifier": "OMN-7"}},
                {"type": "duplicate", "relatedIssue": {"identifier": "OMN-2"}},
                {"type": "duplicate", "relatedIssue": {"identifier": "OMN-3"}},
            ]
        },
    }
    found = read_issue(duplicated)
    assert (found.status, found.duplicate_of) == ("duplicate", "OMN-2")


def test_a_duplicate_relation_that_names_no_survivor_is_a_duplicate_without_a_link() -> (
    None
):
    unnamed = {
        **ISSUE,
        "state": {"name": "Won't do", "type": "canceled"},
        "relations": {"nodes": [{"type": "duplicate", "relatedIssue": None}]},
    }
    found = read_issue(unnamed)
    assert (found.status, found.duplicate_of) == ("duplicate", None)


def test_an_open_ticket_is_open_even_when_it_carries_a_duplicate_relation() -> None:
    linked = {
        **ISSUE,
        "state": {"name": "In Progress", "type": "started"},
        "relations": {
            "nodes": [{"type": "duplicate", "relatedIssue": {"identifier": "OMN-2"}}]
        },
    }
    found = read_issue(linked)
    assert (found.status, found.duplicate_of) == ("open", "OMN-2")


def test_read_of_no_issue_is_missing_and_a_failing_read_is_failed_naming_only_the_type() -> (
    None
):
    assert read_issue(None).state == "missing"
    failed = store(Graphql(error=RuntimeError("token abc"))).read(
        ModelClusterTicketLookupRequest(ticket="OMN-1")
    )
    assert (failed.state, failed.cause) == ("failed", "RuntimeError")


@pytest.mark.parametrize(
    "answer",
    [
        {"data": None, "errors": [{"message": "unauthorized"}]},
        {"data": {"issue": ISSUE}, "errors": [{"message": "partial"}]},
        {},
        {"data": {}},
    ],
)
def test_read_error_answers_are_failed_never_safe_looking_facts(
    answer: Mapping[str, Any],
) -> None:
    found = store(Graphql({READ_QUERY: [answer]})).read(
        ModelClusterTicketLookupRequest(ticket="OMN-1")
    )
    assert found.state == "failed"


@pytest.mark.parametrize("field", ["identifier", "state", "description", "relations"])
def test_a_read_answer_missing_a_field_the_store_reads_is_failed(field: str) -> None:
    bare = {key: value for key, value in ISSUE.items() if key != field}
    assert read_issue(bare).state == "failed"


# ------------------------------------------------------------------ search


def test_search_unions_both_needles_and_follows_their_pages() -> None:
    graphql = Graphql(
        {
            SEARCH_QUERY: [
                page(["OMN-10"], next_cursor="c1", has_next=True),
                page(["OMN-11", "OMN-10"]),
                page(["OMN-2"]),
            ]
        }
    )
    result = store(graphql).search(
        ModelClusterTicketSearchRequest(needles=(SIGNATURE, LEGACY))
    )
    assert (result.state, result.tickets) == ("answered", ("OMN-10", "OMN-11", "OMN-2"))
    assert [post[1]["needle"] for post in graphql.posts] == [
        SIGNATURE,
        SIGNATURE,
        LEGACY,
    ]
    assert [post[1]["after"] for post in graphql.posts] == [None, "c1", None]


def test_a_failing_search_is_failed() -> None:
    failed = store(Graphql(error=OSError("down"))).search(
        ModelClusterTicketSearchRequest(needles=(SIGNATURE,))
    )
    assert (failed.state, failed.cause) == ("failed", "OSError")


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
    result = store(Graphql({SEARCH_QUERY: [answer]})).search(
        ModelClusterTicketSearchRequest(needles=(SIGNATURE,))
    )
    assert (result.state, result.tickets) == ("failed", ())


def test_search_of_an_empty_connection_is_answered_with_no_tickets() -> None:
    result = store(Graphql({SEARCH_QUERY: [page([])]})).search(
        ModelClusterTicketSearchRequest(needles=(SIGNATURE,))
    )
    assert (result.state, result.tickets) == ("answered", ())


def test_a_search_whose_next_page_cannot_be_reached_is_failed() -> None:
    no_cursor = Graphql(
        {SEARCH_QUERY: [page(["OMN-10"], next_cursor=None, has_next=True)]}
    )
    assert (
        store(no_cursor)
        .search(ModelClusterTicketSearchRequest(needles=(SIGNATURE,)))
        .state
        == "failed"
    )
    stuck = Graphql(
        {
            SEARCH_QUERY: [
                page(["OMN-10"], next_cursor="c1", has_next=True),
                page(["OMN-11"], next_cursor="c1", has_next=True),
            ]
        }
    )
    assert (
        store(stuck).search(ModelClusterTicketSearchRequest(needles=(SIGNATURE,))).state
        == "failed"
    )


# ------------------------------------------------------------------ comment


def test_comment_resolves_the_issue_id_by_identifier_and_posts_comment_create_with_it() -> (
    None
):
    graphql = Graphql(
        {
            READ_QUERY: [{"data": {"issue": ISSUE}}],
            COMMENT_MUTATION: [
                {
                    "data": {
                        "commentCreate": {
                            "success": True,
                            "comment": {"id": "comment-1"},
                        }
                    }
                }
            ],
        }
    )
    result = store(graphql).comment(
        ModelClusterTicketCommentRequest(
            ticket="OMN-1", body="Still failing: 2026-11-15"
        )
    )
    assert (result.state, result.comment_id, result.cause) == (
        "commented",
        "comment-1",
        None,
    )
    assert graphql.posts == [
        (READ_QUERY, {"id": "OMN-1"}),
        (
            COMMENT_MUTATION,
            {"issueId": "issue-omn-1", "body": "Still failing: 2026-11-15"},
        ),
    ]


@pytest.mark.parametrize(
    "script",
    [
        # The read that resolves the issue's id fails.
        {READ_QUERY: [{"data": None, "errors": [{"message": "unauthorized"}]}]},
        # The issue the comment targets is gone.
        {READ_QUERY: [{"data": {"issue": None}}]},
        # The mutation answers with errors.
        {
            READ_QUERY: [{"data": {"issue": ISSUE}}],
            COMMENT_MUTATION: [{"data": None, "errors": [{"message": "gone"}]}],
        },
        # The mutation names no comment.
        {
            READ_QUERY: [{"data": {"issue": ISSUE}}],
            COMMENT_MUTATION: [{"data": {"commentCreate": {"success": True}}}],
        },
        # The mutation did not succeed.
        {
            READ_QUERY: [{"data": {"issue": ISSUE}}],
            COMMENT_MUTATION: [
                {"data": {"commentCreate": {"success": False, "comment": None}}}
            ],
        },
    ],
)
def test_a_comment_that_cannot_be_posted_is_failed_never_a_quiet_success(
    script: Mapping[str, list[Mapping[str, Any]]],
) -> None:
    result = store(Graphql(script)).comment(
        ModelClusterTicketCommentRequest(ticket="OMN-1", body="Still failing")
    )
    assert (result.state, result.comment_id) == ("failed", None)


def test_a_raising_comment_is_failed_naming_only_the_type() -> None:
    failed = store(Graphql(error=RuntimeError("token abc"))).comment(
        ModelClusterTicketCommentRequest(ticket="OMN-1", body="Still failing")
    )
    assert (failed.state, failed.cause) == ("failed", "RuntimeError")


# ------------------------------------------------------------------ create, protocol


def test_create_is_the_creators_answer_and_a_raising_creator_is_failed() -> None:
    request = ModelClusterTicketCreateRequest(
        title="[dev-baseline] Test failure cluster: tests.nightly (omnibase_infra)",
        description=f"{SIGNATURE}\n",
        parent="OMN-19998",
    )
    assert store(Graphql()).create(request).ticket == "OMN-9"

    def raising(_: ModelClusterTicketCreateRequest) -> ModelClusterTicketCreateResult:
        raise RuntimeError("token abc")

    failed = store(Graphql(), raising).create(request)
    assert (failed.state, failed.cause) == ("failed", "RuntimeError")


def test_the_store_satisfies_the_cluster_store_protocol() -> None:
    assert isinstance(store(Graphql()), ProtocolClusterTicketStore)
