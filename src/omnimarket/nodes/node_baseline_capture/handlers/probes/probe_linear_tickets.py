"""Linear ticket probe — captures non-completed Linear tickets via HTTP API."""

from __future__ import annotations

import logging
import os
from datetime import datetime

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelLinearTicketSnapshot,
    ProbeSnapshotItem,
)

logger = logging.getLogger(__name__)

_LINEAR_API_URL = "https://api.linear.app/graphql"
_QUERY = """
query {
  issues(filter: {completedAt: {null: true}}, first: 250) {
    nodes {
      identifier
      title
      state { name }
      priority
      assignee { displayName }
      updatedAt
    }
  }
}
"""


class ProbeLinearTickets:
    """Probe that collects non-completed Linear tickets."""

    name: str = "linear_tickets"

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        """Collect Linear tickets using the Linear GraphQL API.

        Reads LINEAR_API_KEY from environment. Returns empty list on failure.
        """
        try:
            import httpx
        except ImportError:
            logger.warning("httpx not available — skipping linear_tickets probe")
            return []

        api_key = os.environ.get("LINEAR_API_KEY", "")
        if not api_key:
            logger.warning("LINEAR_API_KEY not set — skipping linear_tickets probe")
            return []

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    _LINEAR_API_URL,
                    json={"query": _QUERY},
                    headers={"Authorization": api_key},
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning("Linear API call failed: %s", exc)
            return []

        results: list[ProbeSnapshotItem] = []
        nodes = data.get("data", {}).get("issues", {}).get("nodes", [])
        for issue in nodes:
            try:
                updated_at = datetime.fromisoformat(
                    issue["updatedAt"].replace("Z", "+00:00")
                )
                results.append(
                    ModelLinearTicketSnapshot(
                        ticket_id=issue["identifier"],
                        title=issue.get("title", ""),
                        state=issue.get("state", {}).get("name", ""),
                        priority=issue.get("priority"),
                        assignee=(
                            issue["assignee"]["displayName"]
                            if issue.get("assignee")
                            else None
                        ),
                        updated_at=updated_at,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Failed to parse Linear issue %s: %s", issue.get("identifier"), exc
                )
                continue

        return results


__all__: list[str] = ["ProbeLinearTickets"]
