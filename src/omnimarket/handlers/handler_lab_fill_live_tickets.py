# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read live Linear facts for the lab fill (the plugin's idle-slot fill).

The fill calls ``python -m omnimarket.handlers.lab_fill_live_gate_cli`` just before
it sends lanes; one batched Linear read; an unread ticket is never free.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import cast
from urllib.error import URLError

from pydantic import ValidationError

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.handlers.rules_lab_fill_tickets import gate_live_tickets
from omnimarket.models.lab_fill import (
    ModelLabFillLiveFacts,
    ModelLabFillLiveGateRequest,
    ModelLabFillLivePlan,
    ModelLabFillLiveTicket,
)

type Post = Callable[[str, Mapping[str, object]], Mapping[str, object]]

_QUERY = """
query LabFillLiveTickets($numbers: [Float!], $first: Int) {
  viewer { id }
  issues(first: $first, filter: {team: {key: {eq: "OMN"}}, number: {in: $numbers}}) {
    nodes {
      identifier
      state { type name }
      assignee { id name }
    }
  }
}
"""


class LabFillLiveReadError(Exception):
    """Linear could not supply a readable live ticket response."""


def linear_post(query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
    """Post one Linear GraphQL read without exposing the API key in diagnostics."""
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise LabFillLiveReadError("LINEAR_API_KEY is missing")
    try:
        request = urllib.request.Request(
            LINEAR_GRAPHQL_URL,
            data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
            headers={"Authorization": key, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(  # no-contract-check: Linear port boundary
            request, timeout=20
        ) as response:
            payload: object = json.load(response)
    except (OSError, ValueError, URLError) as exc:
        raise LabFillLiveReadError(f"Linear unreadable: {type(exc).__name__}") from None
    if not isinstance(payload, dict):
        raise LabFillLiveReadError("Linear returned no data")
    if payload.get("errors"):
        raise LabFillLiveReadError("Linear GraphQL errors")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise LabFillLiveReadError("Linear returned no data")
    return cast("Mapping[str, object]", data)


def _object(value: object, field: str) -> Mapping[str, object]:
    """Require a GraphQL object before reading its fields."""
    if not isinstance(value, dict):
        raise LabFillLiveReadError(f"Linear returned malformed {field}")
    return cast("Mapping[str, object]", value)


def read_live_tickets(
    tickets: Sequence[str], *, post: Post = linear_post
) -> tuple[tuple[ModelLabFillLiveTicket, ...], str]:
    """Read tickets and viewer once, retaining only requested ids in request order."""
    if not tickets:
        return (), ""
    answer = post(
        _QUERY,
        {
            "numbers": [int(ticket.removeprefix("OMN-")) for ticket in tickets],
            "first": len(tickets),
        },
    )
    issues = _object(answer.get("issues"), "issues")
    nodes = issues.get("nodes")
    if not isinstance(nodes, list):
        raise LabFillLiveReadError("Linear returned malformed nodes")
    viewer = _object(answer.get("viewer", {}), "viewer").get("id", "")
    if not isinstance(viewer, str):
        raise LabFillLiveReadError("Linear returned malformed viewer id")
    requested = set(tickets)
    live: dict[str, ModelLabFillLiveTicket] = {}
    for raw_node in nodes:
        node = _object(raw_node, "ticket")
        identifier = node.get("identifier")
        if not isinstance(identifier, str):
            raise LabFillLiveReadError("Linear returned malformed identifier")
        if identifier not in requested:
            continue
        raw_state = node.get("state")
        state = {} if raw_state is None else _object(raw_state, "state")
        raw_assignee = node.get("assignee")
        assignee = {} if raw_assignee is None else _object(raw_assignee, "assignee")
        try:
            live[identifier] = ModelLabFillLiveTicket.model_validate(
                {
                    "id": identifier,
                    "state_type": state.get("type", ""),
                    "state_name": state.get("name", ""),
                    "assignee_id": assignee.get("id"),
                    "assignee_name": assignee.get("name"),
                }
            )
        except ValidationError:
            raise LabFillLiveReadError("Linear returned malformed ticket") from None
    return tuple(live[ticket] for ticket in tickets if ticket in live), viewer


class HandlerLabFillLiveGate:
    """Supply fresh Linear facts to the pure pre-send gate."""

    def __init__(self, post: Post | None = None) -> None:
        self._post = linear_post if post is None else post

    def handle(self, request: ModelLabFillLiveGateRequest) -> ModelLabFillLivePlan:
        """Fail closed on an unreadable live read and recognize the Linear viewer."""
        viewer = ""
        error = ""
        try:
            live, viewer = read_live_tickets(request.tickets, post=self._post)
        except LabFillLiveReadError as exc:
            error = str(exc)
            live = ()
        operators = tuple(value for value in (request.operator, viewer) if value)
        return gate_live_tickets(
            ModelLabFillLiveFacts(
                tickets=request.tickets, operators=operators, live=live, error=error
            )
        )
