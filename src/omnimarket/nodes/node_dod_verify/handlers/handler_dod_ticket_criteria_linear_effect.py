# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed Linear read of a ticket's body for the criteria-drift check (OMN-20858).

An EFFECT: the one Linear round trip ``node_dod_verify`` makes. It differs from the
OCC transcriber's reader on purpose. That reader returns an empty mapping on every
failure because an outage may cost bindings but never a mint; this one is the
opposite case, where an unreadable ticket must refuse, so a failure is a typed
:class:`ModelTicketCriteriaRead` carrying the reason and never an empty body.

The credential is read from the environment by name and is never logged, returned
or placed in a reason; a reason names the failure class only, because a urllib error
can carry the request it failed on, headers included.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Final

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.nodes.node_dod_verify.models.model_ticket_criteria_read import (
    ModelTicketCriteriaRead,
)
from omnimarket.occ_creation_revision import LINEAR_API_KEY_ENV

__all__ = ["LINEAR_READ_TIMEOUT_S", "HandlerDodTicketCriteriaLinearEffect"]

#: Ceiling on one read. Two reads bracket a verification, so a hung read must not
#: stretch it.
LINEAR_READ_TIMEOUT_S: Final[float] = 30.0

_ISSUE_BODY_QUERY: Final[str] = """
query($id: String!) {
  issue(id: $id) {
    identifier
    description
  }
}
"""


class HandlerDodTicketCriteriaLinearEffect:
    """Reads one ticket's current description from Linear."""

    def read(self, ticket_id: str) -> ModelTicketCriteriaRead:
        """The ticket body, or the reason it could not be read."""
        api_key = os.environ.get(LINEAR_API_KEY_ENV, "")
        if not api_key:
            return self._unavailable(
                ticket_id, "no Linear credential in the environment"
            )
        payload = json.dumps(
            {"query": _ISSUE_BODY_QUERY, "variables": {"id": ticket_id}}
        ).encode("utf-8")
        request = urllib.request.Request(
            LINEAR_GRAPHQL_URL,
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": api_key},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=LINEAR_READ_TIMEOUT_S
            ) as response:
                body = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            return self._unavailable(
                ticket_id, f"Linear read failed ({type(error).__name__})"
            )
        if not isinstance(body, dict):
            return self._unavailable(ticket_id, "Linear returned a non-object body")
        if body.get("errors"):
            return self._unavailable(ticket_id, "Linear returned a GraphQL error")
        data = body.get("data")
        issue = data.get("issue") if isinstance(data, dict) else None
        if not isinstance(issue, dict):
            return self._unavailable(ticket_id, "Linear holds no such ticket")
        if issue.get("identifier") != ticket_id:
            return self._unavailable(ticket_id, "Linear returned a different ticket")
        description = issue.get("description")
        return ModelTicketCriteriaRead(
            ticket_id=ticket_id,
            description=description if isinstance(description, str) else "",
        )

    @staticmethod
    def _unavailable(ticket_id: str, reason: str) -> ModelTicketCriteriaRead:
        return ModelTicketCriteriaRead(ticket_id=ticket_id, unavailable_reason=reason)
