"""ProbeLinearTickets — collect non-completed Linear ticket snapshots via HTTP API."""

from __future__ import annotations

import logging
import os
from datetime import datetime

import httpx

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelLinearTicketSnapshot,
)

logger = logging.getLogger(__name__)

_LINEAR_API_URL = "https://api.linear.app/graphql"
_HTTP_TIMEOUT_SECONDS = 10.0
_MAX_TICKETS = 500

_QUERY = """
query NonCompletedIssues($first: Int!, $after: String) {
  issues(
    first: $first
    after: $after
    filter: {
      completedAt: { null: true }
      canceledAt: { null: true }
    }
  ) {
    nodes {
      identifier
      title
      state { name }
      priority
      assignee { displayName }
      updatedAt
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class ProbeLinearTickets:
    """Collect non-completed Linear ticket snapshots."""

    name: str = "linear_tickets"

    async def collect(self) -> list[ModelLinearTicketSnapshot]:
        """Return ticket snapshots; returns empty list on any failure."""
        api_key = os.environ.get("LINEAR_API_KEY", "")
        if not api_key:
            logger.warning("probe_linear_tickets: LINEAR_API_KEY not set, skipping")
            return []

        snapshots: list[ModelLinearTicketSnapshot] = []
        headers = {"Authorization": api_key, "Content-Type": "application/json"}
        cursor: str | None = None

        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                while len(snapshots) < _MAX_TICKETS:
                    variables: dict[str, object] = {"first": 100}
                    if cursor:
                        variables["after"] = cursor

                    resp = client.post(
                        _LINEAR_API_URL,
                        json={"query": _QUERY, "variables": variables},
                        headers=headers,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    issues_data = data.get("data", {}).get("issues", {})
                    nodes = issues_data.get("nodes", [])
                    page_info = issues_data.get("pageInfo", {})

                    for node in nodes:
                        updated_at_str = node.get("updatedAt", "")
                        try:
                            updated_at = datetime.fromisoformat(
                                updated_at_str.replace("Z", "+00:00")
                            )
                        except (ValueError, TypeError):
                            updated_at = datetime.now()

                        assignee_obj = node.get("assignee") or {}
                        assignee: str | None = assignee_obj.get("displayName")

                        state_obj = node.get("state") or {}
                        state = str(state_obj.get("name", ""))

                        priority_raw = node.get("priority")
                        priority: int | None = (
                            int(priority_raw) if priority_raw is not None else None
                        )

                        snapshots.append(
                            ModelLinearTicketSnapshot(
                                ticket_id=str(node.get("identifier", "")),
                                title=str(node.get("title", "")),
                                state=state,
                                priority=priority,
                                assignee=assignee,
                                updated_at=updated_at,
                            )
                        )

                    if not page_info.get("hasNextPage"):
                        break
                    cursor = page_info.get("endCursor")
                    if not cursor:
                        break

        except (httpx.HTTPError, httpx.TimeoutException, OSError, ValueError) as exc:
            logger.warning("probe_linear_tickets failed: %s", exc)
            return []

        return snapshots


__all__: list[str] = ["ProbeLinearTickets"]
