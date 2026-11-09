# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Linear project tracker the ticketing nodes read tickets through (OMN-21676).

``node_create_ticket`` builds ``AdapterLinearGraphQLProjectTracker`` from the key its own
contract declares, resolved through the secret store at the effect boundary (Infisical on the
lab). A node that only needs to read a ticket's body takes the same tracker the same way, so
no handler reads a Linear credential from the environment by name and none opens its own
GraphQL connection.

The credential is the caller node's contract-declared ``LINEAR_API_KEY`` ref. It is resolved
here, handed to the adapter and never returned, logged or placed in an error message.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from omnibase_infra.adapters.project_tracker.linear_graphql_project_tracker_adapter import (
    AdapterLinearGraphQLProjectTracker,
)

from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.models.model_criteria_drift import ModelTicketDescriptionEdit
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_dod_verify.services.criteria_ticket_read import (
    ProtocolCriteriaIssue,
)

__all__ = ["LINEAR_SECRET_NAME", "LinearCriteriaTracker", "linear_project_tracker"]

LINEAR_SECRET_NAME = "LINEAR_API_KEY"


_HISTORY_QUERY = (
    "query($id: String!) { issue(id: $id) { history(first: 250) { nodes "
    "{ createdAt updatedDescription actor { name } } } } }"
)
_HISTORY_UNKNOWN_ACTOR = "unknown-actor"


class LinearCriteriaTracker:
    """The Linear adapter plus the history read the drift writer needs (OMN-23184)."""

    def __init__(self, adapter: AdapterLinearGraphQLProjectTracker) -> None:
        self._adapter = adapter

    async def get_issue(self, issue_id: str) -> ProtocolCriteriaIssue:
        return await self._adapter.get_issue(issue_id)

    async def description_edits(
        self, issue_id: str
    ) -> Sequence[ModelTicketDescriptionEdit]:
        """History nodes where ``updatedDescription`` is true; raises when unreadable."""
        response = await self._adapter.post_graphql(_HISTORY_QUERY, {"id": issue_id})
        if response.status_code != _HTTP_OK:
            raise RuntimeError(f"Linear history HTTP {response.status_code}")
        body = response.json()
        issue = (
            (body.get("data") or {}).get("issue") if isinstance(body, dict) else None
        )
        if not isinstance(issue, dict) or body.get("errors"):
            raise RuntimeError("Linear history carried no issue")
        nodes = (issue.get("history") or {}).get("nodes")
        if not isinstance(nodes, list):
            raise RuntimeError("Linear history carried no nodes")
        edits: list[ModelTicketDescriptionEdit] = []
        for node in nodes:
            if not isinstance(node, dict) or node.get("updatedDescription") is not True:
                continue
            actor = node.get("actor")
            name = actor.get("name") if isinstance(actor, dict) else None
            edits.append(
                ModelTicketDescriptionEdit(
                    actor=name
                    if isinstance(name, str) and name
                    else _HISTORY_UNKNOWN_ACTOR,
                    created_at=node["createdAt"],
                )
            )
        return edits

    async def close(self) -> None:
        await self._adapter.close()


_HTTP_OK = 200


async def linear_project_tracker(
    contract_path: Path, *, secret_name: str = LINEAR_SECRET_NAME
) -> LinearCriteriaTracker:
    """The Linear tracker keyed by the secret ``contract_path`` declares.

    Raises ``ValueError`` when the contract does not declare the secret and
    ``RuntimeError`` when the secret store holds no value for it. The caller closes the
    tracker.
    """
    ref = contract_secret_ref(contract_path, secret_name)
    secret = await resolve_api_key_async(ref, env_var_fallback=ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None; set it in the secret store"
        )
    return LinearCriteriaTracker(
        AdapterLinearGraphQLProjectTracker(api_key=secret.get_secret_value())
    )
