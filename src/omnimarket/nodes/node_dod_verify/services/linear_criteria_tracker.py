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
from datetime import datetime
from pathlib import Path

from omnibase_infra.adapters.project_tracker.linear_graphql_project_tracker_adapter import (
    AdapterLinearGraphQLProjectTracker,
)

from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.models.model_criteria_drift_writer import ModelDescriptionEdit
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_dod_verify.services.criteria_ticket_read import (
    ProtocolCriteriaIssue,
)

__all__ = [
    "LINEAR_SECRET_NAME",
    "LinearCriteriaTracker",
    "linear_criteria_tracker",
    "linear_project_tracker",
]

_HISTORY_PAGE = 100
# 20 pages of 100 entries: a ticket with more history than this is unmeasured, not truncated.
_HISTORY_PAGES = 20
_QUERY_DESCRIPTION_HISTORY = f"""
query CriteriaDescriptionHistory($id: String!, $after: String) {{
  issue(id: $id) {{
    history(first: {_HISTORY_PAGE}, after: $after) {{
      nodes {{ createdAt updatedDescription actor {{ name displayName }} }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}
"""

LINEAR_SECRET_NAME = "LINEAR_API_KEY"


async def linear_project_tracker(
    contract_path: Path, *, secret_name: str = LINEAR_SECRET_NAME
) -> AdapterLinearGraphQLProjectTracker:
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
    return AdapterLinearGraphQLProjectTracker(api_key=secret.get_secret_value())


async def linear_criteria_tracker(
    contract_path: Path, *, secret_name: str = LINEAR_SECRET_NAME
) -> LinearCriteriaTracker:
    """The Linear tracker for the criteria check: the ticket read and the history read."""
    adapter = await linear_project_tracker(contract_path, secret_name=secret_name)
    return LinearCriteriaTracker(adapter)


class LinearCriteriaTracker:
    """``ProtocolCriteriaHistoryTracker`` over the Linear project tracker.

    The ticket read and ``close`` are the adapter's. The history read is one paged GraphQL
    query over ``issue.history`` through the adapter's own transport, keeping the nodes where
    ``updatedDescription`` is true.
    """

    def __init__(self, adapter: AdapterLinearGraphQLProjectTracker) -> None:
        self._adapter = adapter

    async def get_issue(self, issue_id: str) -> ProtocolCriteriaIssue:
        """The ticket, as the adapter reads it."""
        return await self._adapter.get_issue(issue_id)

    async def close(self) -> None:
        """Release the adapter's connection."""
        await self._adapter.close()

    async def description_edits(self, issue_id: str) -> Sequence[ModelDescriptionEdit]:
        """Every history entry that rewrote the description; raises when the history is unread."""
        edits: list[ModelDescriptionEdit] = []
        after: str | None = None
        for _ in range(_HISTORY_PAGES):
            variables: dict[str, object] = {"id": issue_id}
            if after is not None:
                variables["after"] = after
            response = await self._adapter.post_graphql(
                _QUERY_DESCRIPTION_HISTORY, variables
            )
            if response.status_code != 200:
                raise RuntimeError(f"history read answered HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("errors"):
                raise RuntimeError("history read answered with errors")
            data = payload.get("data")
            issue = data.get("issue") if isinstance(data, dict) else None
            history = issue.get("history") if isinstance(issue, dict) else None
            if not isinstance(history, dict):
                raise RuntimeError("history read found no history")
            for node in history.get("nodes") or []:
                if isinstance(node, dict) and node.get("updatedDescription") is True:
                    edits.append(_edit(node))
            page = history.get("pageInfo")
            if not isinstance(page, dict) or not page.get("hasNextPage"):
                return tuple(edits)
            cursor = page.get("endCursor")
            if not isinstance(cursor, str):
                raise RuntimeError("history read gave no next cursor")
            after = cursor
        raise RuntimeError("history is longer than the pages read")


def _edit(node: dict[str, object]) -> ModelDescriptionEdit:
    created_at = node.get("createdAt")
    if not isinstance(created_at, str):
        raise RuntimeError("a history entry carries no time")
    actor = node.get("actor")
    name: object = None
    if isinstance(actor, dict):
        name = actor.get("displayName") or actor.get("name")
    return ModelDescriptionEdit(
        actor=name if isinstance(name, str) and name else "unnamed-actor",
        created_at=datetime.fromisoformat(created_at),
    )
