# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend cluster ticket store: search and read over Linear's GraphQL, comment on it, create through an injected creator.

The creator is the seam to ``node_create_ticket``'s create (the gate runs inside it, once): the binding site that builds
this store hands it a callable that takes the create request and answers ``created``, ``refused`` (the gate's codes) or
``failed``. This module imports no other node. The GraphQL reader is injected too, so a test answers from a dict and the
lab binds the one that holds the Linear key from the secret store.

Fail closed: an answer that carries GraphQL ``errors``, or lacks a field the store reads, is ``failed``, never a
default that looks safe. Only an issue the answer names as ``null`` is ``missing``, and only a connection the answer
shows empty with no next page is an empty search. The comment resolves the issue's id by its identifier (the read
query returns it) and posts ``commentCreate`` with that id.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_cluster_ticket_store import (
    ModelClusterTicketCommentRequest,
    ModelClusterTicketCommentResult,
    ModelClusterTicketCreateRequest,
    ModelClusterTicketCreateResult,
    ModelClusterTicketLookupRequest,
    ModelClusterTicketLookupResult,
    ModelClusterTicketSearchRequest,
    ModelClusterTicketSearchResult,
)

SEARCH_PAGE = 50

READ_QUERY = """
query ClusterTicketRead($id: String!) {
  issue(id: $id) {
    id
    identifier
    state { name type }
    description
    relations { nodes { type relatedIssue { identifier } } }
  }
}
"""

SEARCH_QUERY = """
query ClusterTicketSearch($needle: String!, $first: Int!, $after: String) {
  issues(first: $first, after: $after, filter: { description: { contains: $needle } }) {
    nodes { identifier }
    pageInfo { hasNextPage endCursor }
  }
}
"""

COMMENT_MUTATION = """
mutation ClusterTicketComment($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment { id }
  }
}
"""


class ProtocolLinearGraphql(Protocol):
    """Post one GraphQL document and return the decoded response (``{"data": ...}``); raises when it cannot."""

    def post(
        self, query: str, variables: Mapping[str, object]
    ) -> Mapping[str, Any]: ...


class GraphqlAnswerError(Exception):
    """The decoded answer holds GraphQL errors or lacks a field the store must read."""


def _field(container: object, name: str) -> Any:
    """``container[name]``, raising when the container is no mapping or the key is absent (``null`` is a value)."""
    if not isinstance(container, Mapping) or name not in container:
        raise GraphqlAnswerError(name)
    return container[name]


def _data(answer: Mapping[str, Any]) -> Mapping[str, Any]:
    """The ``data`` object of an answer that carries no GraphQL errors."""
    if answer.get("errors"):
        raise GraphqlAnswerError("errors")
    data = _field(answer, "data")
    if not isinstance(data, Mapping):
        raise GraphqlAnswerError("data")
    return data


def _nodes(connection: object) -> list[Any]:
    nodes = _field(connection, "nodes")
    if not isinstance(nodes, list):
        raise GraphqlAnswerError("nodes")
    return nodes


def _duplicate_of(relations: list[Any]) -> tuple[bool, str | None]:
    """Whether a duplicate relation exists, and the surviving ticket the first one names."""
    for relation in relations:
        if str(_field(relation, "type") or "") != "duplicate":
            continue
        related = _field(relation, "relatedIssue")
        if related is None:
            return True, None
        return True, str(_field(related, "identifier") or "") or None
    return False, None


def _status(state: object, duplicate_relation: bool) -> str:
    """The cluster status: the state's type ends the ticket, its display name only names a duplicate."""
    kind = str(_field(state, "type") or "")
    if kind == "completed":
        return "done"
    if kind == "canceled":
        name = str(_field(state, "name") or "")
        if name.lower() == "duplicate" or duplicate_relation:
            return "duplicate"
        return "canceled"
    return "open"


@dataclass(frozen=True)
class GraphqlClusterTicketStore:
    """``search``, ``read`` and ``comment`` over GraphQL; ``create`` is the injected creator's answer."""

    synchronous_execution: ClassVar[bool] = True

    graphql: ProtocolLinearGraphql
    creator: Callable[[ModelClusterTicketCreateRequest], ModelClusterTicketCreateResult]

    def search(
        self, request: ModelClusterTicketSearchRequest
    ) -> ModelClusterTicketSearchResult:
        """One paginated query per needle, the identifiers of every page unioned in discovery order."""
        try:
            tickets: list[str] = []
            seen: set[str] = set()
            for needle in request.needles:
                after: str | None = None
                seen_cursors: set[str] = set()
                while True:
                    data = _data(
                        self.graphql.post(
                            SEARCH_QUERY,
                            {"needle": needle, "first": SEARCH_PAGE, "after": after},
                        )
                    )
                    issues = _field(data, "issues")
                    for node in _nodes(issues):
                        identifier = str(_field(node, "identifier"))
                        if identifier not in seen:
                            seen.add(identifier)
                            tickets.append(identifier)
                    page = _field(issues, "pageInfo")
                    if not _field(page, "hasNextPage"):
                        break
                    after = _field(page, "endCursor")
                    if not isinstance(after, str) or after in seen_cursors:
                        raise GraphqlAnswerError("endCursor")
                    seen_cursors.add(after)
            return ModelClusterTicketSearchResult(
                state="answered", tickets=tuple(tickets)
            )
        except Exception as error:
            return ModelClusterTicketSearchResult(
                state="failed", cause=type(error).__name__
            )

    def read(
        self, request: ModelClusterTicketLookupRequest
    ) -> ModelClusterTicketLookupResult:
        try:
            data = _data(self.graphql.post(READ_QUERY, {"id": request.ticket}))
            issue = _field(data, "issue")
            if issue is None:
                return ModelClusterTicketLookupResult(state="missing")
            has_duplicate, duplicate_of = _duplicate_of(
                _nodes(_field(issue, "relations"))
            )
            return ModelClusterTicketLookupResult(
                state="found",
                ticket=str(_field(issue, "identifier")),
                status=_status(_field(issue, "state"), has_duplicate),
                description=_field(issue, "description") or "",
                duplicate_of=duplicate_of,
            )
        except Exception as error:
            return ModelClusterTicketLookupResult(
                state="failed", cause=type(error).__name__
            )

    def create(
        self, request: ModelClusterTicketCreateRequest
    ) -> ModelClusterTicketCreateResult:
        try:
            return self.creator(request)
        except Exception as error:
            return ModelClusterTicketCreateResult(
                state="failed", cause=type(error).__name__
            )

    def comment(
        self, request: ModelClusterTicketCommentRequest
    ) -> ModelClusterTicketCommentResult:
        """Resolve the issue's id by its identifier, then post ``commentCreate`` with it."""
        try:
            data = _data(self.graphql.post(READ_QUERY, {"id": request.ticket}))
            issue = _field(data, "issue")
            if issue is None:
                raise GraphqlAnswerError("issue")
            answer = _data(
                self.graphql.post(
                    COMMENT_MUTATION,
                    {"issueId": str(_field(issue, "id")), "body": request.body},
                )
            )
            created = _field(answer, "commentCreate")
            if not _field(created, "success"):
                raise GraphqlAnswerError("success")
            comment = _field(created, "comment")
            return ModelClusterTicketCommentResult(
                state="commented", comment_id=str(_field(comment, "id"))
            )
        except Exception as error:
            return ModelClusterTicketCommentResult(
                state="failed", cause=type(error).__name__
            )
