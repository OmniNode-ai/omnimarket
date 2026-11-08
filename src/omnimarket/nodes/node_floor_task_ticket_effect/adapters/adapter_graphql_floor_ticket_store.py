# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend ticket store: read and search over Linear's GraphQL, create through an injected creator.

The creator is the seam to ``node_create_ticket``'s create (the gate runs inside it, once): the binding site that builds
this store hands it a callable that takes the create request and answers ``created``, ``refused`` (the gate's codes) or
``failed``. This module imports no other node. The GraphQL reader is injected too, so a test answers from a dict and the
lab binds the one that holds the Linear key from the secret store.

Fail closed: an answer that carries GraphQL ``errors``, or lacks a field the judge reads, is ``failed``, never a
default that looks safe. Only an issue the answer names as ``null`` is ``missing``, and only a connection the answer
shows empty with no next page is an empty search.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from omnibase_spi.contracts.services import (
    ModelFloorTicketCreateRequest,
    ModelFloorTicketCreateResult,
    ModelFloorTicketLookupRequest,
    ModelFloorTicketLookupResult,
    ModelFloorTicketSearchRequest,
    ModelFloorTicketSearchResult,
)

FLOOR_KEY_LINE = "Floor key: {floor_key}"
SEARCH_PAGE = 50
# Linear's workflow-state types that end a ticket; the state's display name is configurable, its type is not.
TERMINAL_STATE_TYPES = frozenset({"completed", "canceled"})

READ_QUERY = """
query FloorTicketRead($id: String!) {
  issue(id: $id) {
    identifier
    state { name type }
    description
    project { name }
    labels { nodes { name } }
    children(first: 1) { nodes { id } }
  }
}
"""

SEARCH_QUERY = """
query FloorTicketSearch($needle: String!, $first: Int!, $after: String) {
  issues(first: $first, after: $after, filter: { description: { contains: $needle } }) {
    nodes { identifier }
    pageInfo { hasNextPage endCursor }
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


def _status(state: object) -> str:
    """The status the judge reads: the state's type when it ends the ticket, else the state's display name."""
    kind = str(_field(state, "type") or "")
    return kind if kind in TERMINAL_STATE_TYPES else str(_field(state, "name") or "")


def _nodes(connection: object) -> list[Any]:
    nodes = _field(connection, "nodes")
    if not isinstance(nodes, list):
        raise GraphqlAnswerError("nodes")
    return nodes


@dataclass(frozen=True)
class GraphqlFloorTicketStore:
    """``read`` and ``search_by_floor_key`` over GraphQL; ``create`` is the injected creator's answer."""

    synchronous_execution: ClassVar[bool] = True

    graphql: ProtocolLinearGraphql
    creator: Callable[[ModelFloorTicketCreateRequest], ModelFloorTicketCreateResult]

    def read(
        self, request: ModelFloorTicketLookupRequest
    ) -> ModelFloorTicketLookupResult:
        try:
            data = _data(self.graphql.post(READ_QUERY, {"id": request.ticket}))
            issue = _field(data, "issue")
            if issue is None:
                return ModelFloorTicketLookupResult(state="missing")
            project = _field(issue, "project")
            return ModelFloorTicketLookupResult(
                state="found",
                ticket=str(_field(issue, "identifier")),
                status=_status(_field(issue, "state")),
                description=_field(issue, "description") or "",
                project=(
                    None if project is None else str(_field(project, "name") or "")
                )
                or None,
                labels=tuple(
                    str(_field(label, "name"))
                    for label in _nodes(_field(issue, "labels"))
                ),
                has_children=bool(_nodes(_field(issue, "children"))),
            )
        except Exception as error:
            return ModelFloorTicketLookupResult(
                state="failed", cause=type(error).__name__
            )

    def search_by_floor_key(
        self, request: ModelFloorTicketSearchRequest
    ) -> ModelFloorTicketSearchResult:
        needle = FLOOR_KEY_LINE.format(floor_key=request.floor_key)
        try:
            tickets: list[str] = []
            seen_cursors: set[str] = set()
            after: str | None = None
            while True:
                data = _data(
                    self.graphql.post(
                        SEARCH_QUERY,
                        {"needle": needle, "first": SEARCH_PAGE, "after": after},
                    )
                )
                issues = _field(data, "issues")
                tickets.extend(
                    str(_field(node, "identifier")) for node in _nodes(issues)
                )
                page = _field(issues, "pageInfo")
                if not _field(page, "hasNextPage"):
                    break
                after = _field(page, "endCursor")
                if not isinstance(after, str) or after in seen_cursors:
                    raise GraphqlAnswerError("endCursor")
                seen_cursors.add(after)
            return ModelFloorTicketSearchResult(
                state="answered", tickets=tuple(tickets)
            )
        except Exception as error:
            return ModelFloorTicketSearchResult(
                state="failed", cause=type(error).__name__
            )

    def create(
        self, request: ModelFloorTicketCreateRequest
    ) -> ModelFloorTicketCreateResult:
        try:
            return self.creator(request)
        except Exception as error:
            return ModelFloorTicketCreateResult(
                state="failed", cause=type(error).__name__
            )
