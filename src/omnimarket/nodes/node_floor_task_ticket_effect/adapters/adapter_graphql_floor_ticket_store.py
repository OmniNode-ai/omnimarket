# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend ticket store: read and search over Linear's GraphQL, create through an injected creator.

The creator is the seam to ``node_create_ticket``'s create (the gate runs inside it, once): the binding site that builds
this store hands it a callable that takes the create request and answers ``created``, ``refused`` (the gate's codes) or
``failed``. This module imports no other node. The GraphQL reader is injected too, so a test answers from a dict and the
lab binds the one that holds the Linear key from the secret store.
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

READ_QUERY = """
query FloorTicketRead($id: String!) {
  issue(id: $id) {
    identifier
    state { name }
    description
    project { name }
    labels { nodes { name } }
    children(first: 1) { nodes { id } }
  }
}
"""

SEARCH_QUERY = """
query FloorTicketSearch($needle: String!, $first: Int!) {
  issues(first: $first, filter: { description: { contains: $needle } }) {
    nodes { identifier }
  }
}
"""


class ProtocolLinearGraphql(Protocol):
    """Post one GraphQL document and return the decoded response (``{"data": ...}``); raises when it cannot."""

    def post(
        self, query: str, variables: Mapping[str, object]
    ) -> Mapping[str, Any]: ...


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
            data = self.graphql.post(READ_QUERY, {"id": request.ticket})
            issue = (data.get("data") or {}).get("issue")
            if issue is None:
                return ModelFloorTicketLookupResult(state="missing")
            return ModelFloorTicketLookupResult(
                state="found",
                ticket=str(issue["identifier"]),
                status=str((issue.get("state") or {}).get("name") or ""),
                description=issue.get("description") or "",
                project=(issue.get("project") or {}).get("name") or None,
                labels=tuple(
                    str(label["name"])
                    for label in (issue.get("labels") or {}).get("nodes") or []
                ),
                has_children=bool((issue.get("children") or {}).get("nodes")),
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
            data = self.graphql.post(
                SEARCH_QUERY, {"needle": needle, "first": SEARCH_PAGE}
            )
            nodes = ((data.get("data") or {}).get("issues") or {}).get("nodes") or []
            return ModelFloorTicketSearchResult(
                state="answered",
                tickets=tuple(str(node["identifier"]) for node in nodes),
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
