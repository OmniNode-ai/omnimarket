# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapter that fetches tickets from Linear and returns them as scored tickets.

Implements ProtocolRsdFillHandler for live build loop execution.
Queries Linear for Active Sprint tickets in Backlog/Todo status,
scores them by priority, and returns top-N.

Auto-discovers team ID if LINEAR_TEAM_ID is not set.

Related:
    - OMN-7810: Wire build loop to Linear queue
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

import httpx

from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    RsdFillResult,
    ScoredTicket,
)

logger = logging.getLogger(__name__)

# Linear GraphQL endpoint
_LINEAR_API_URL = "https://api.linear.app/graphql"

# Priority mapping: Linear uses 0=none, 1=urgent, 2=high, 3=medium, 4=low
# RSD score inversely correlates with priority number (urgent = highest score)
_PRIORITY_TO_RSD: dict[int, float] = {
    0: 0.5,  # No priority
    1: 1.0,  # Urgent
    2: 0.8,  # High
    3: 0.6,  # Medium
    4: 0.4,  # Low
}

_QUERY_TEAMS = """
query {
  teams {
    nodes {
      id
      name
      key
    }
  }
}
"""

_QUERY_ACTIVE_TICKETS = """
query ActiveSprintTickets($teamId: ID!, $limit: Int!) {
  issues(
    filter: {
      team: { id: { eq: $teamId } }
      cycle: { isActive: { eq: true } }
      state: { name: { in: ["Backlog", "Todo"] } }
    }
    first: $limit
    orderBy: updatedAt
  ) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      labels { nodes { name } }
      children { nodes { id } }
    }
  }
}
"""

# Fallback: fetch all backlog/todo from team regardless of cycle
_QUERY_BACKLOG_TICKETS = """
query BacklogTickets($teamId: ID!, $limit: Int!) {
  issues(
    filter: {
      team: { id: { eq: $teamId } }
      state: { name: { in: ["Backlog", "Todo"] } }
    }
    first: $limit
    orderBy: updatedAt
  ) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      labels { nodes { name } }
      children { nodes { id } }
    }
  }
}
"""

# Labels that mark a ticket as a container (epic / parent), even if Linear
# has no children yet. Leaf work should never use these labels.
_EPIC_LABEL_NAMES: frozenset[str] = frozenset({"epic", "meta", "parent"})


class AdapterLinearFill:
    """Fetches tickets from Linear Active Sprint and scores by priority.

    Implements ProtocolRsdFillHandler for live orchestrator wiring.
    Auto-discovers team ID if not provided.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        team_id: str | None = None,
        team_key: str | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("LINEAR_API_KEY", "")
        self._team_id = team_id or os.environ.get("LINEAR_TEAM_ID", "")
        self._team_key = team_key or os.environ.get("LINEAR_TEAM_KEY", "OMN")
        self._team_id_resolved = False

    async def handle(
        self,
        *,
        correlation_id: UUID,
        scored_tickets: tuple[ScoredTicket, ...] = (),
        max_tickets: int = 5,
    ) -> RsdFillResult:
        """Fetch top-N tickets from Linear Active Sprint.

        If pre-scored tickets are provided, delegates to the pure compute
        path (select top-N from provided). Otherwise fetches from Linear.
        """
        if scored_tickets:
            sorted_tickets = sorted(
                scored_tickets,
                key=lambda t: (-t.rsd_score, t.priority, t.ticket_id),
            )
            selected = tuple(sorted_tickets[:max_tickets])
            return RsdFillResult(
                selected_tickets=selected,
                total_selected=len(selected),
            )

        if not self._api_key:
            logger.warning(
                "LINEAR_API_KEY not set — returning empty fill result "
                "(correlation_id=%s)",
                correlation_id,
            )
            return RsdFillResult(selected_tickets=(), total_selected=0)

        # Auto-discover team ID if needed
        if not self._team_id and not self._team_id_resolved:
            await self._discover_team_id()

        if not self._team_id:
            logger.warning(
                "Could not resolve Linear team ID — returning empty fill result "
                "(correlation_id=%s)",
                correlation_id,
            )
            return RsdFillResult(selected_tickets=(), total_selected=0)

        try:
            tickets = await self._fetch_from_linear(max_tickets)
            logger.info(
                "Linear fill: fetched %d tickets (correlation_id=%s)",
                len(tickets),
                correlation_id,
            )
            return RsdFillResult(
                selected_tickets=tuple(tickets),
                total_selected=len(tickets),
            )
        except Exception as exc:
            logger.exception(
                "Linear fill failed (correlation_id=%s): %s",
                correlation_id,
                exc,
            )
            return RsdFillResult(selected_tickets=(), total_selected=0)

    async def _discover_team_id(self) -> None:
        """Auto-discover team ID from Linear API using team key."""
        self._team_id_resolved = True
        try:
            headers = {
                "Authorization": self._api_key,
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    _LINEAR_API_URL,
                    json={"query": _QUERY_TEAMS},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()

            teams = data.get("data", {}).get("teams", {}).get("nodes", [])
            for team in teams:
                if team.get("key") == self._team_key:
                    self._team_id = team["id"]
                    logger.info(
                        "Discovered team ID for %s: %s (%s)",
                        self._team_key,
                        self._team_id,
                        team.get("name", ""),
                    )
                    return

            # If exact key not found, use first team
            if teams:
                self._team_id = teams[0]["id"]
                logger.warning(
                    "Team key %s not found, using first team: %s (%s)",
                    self._team_key,
                    self._team_id,
                    teams[0].get("name", ""),
                )
        except Exception as exc:
            logger.warning("Failed to discover team ID: %s", exc)

    async def _fetch_from_linear(self, limit: int) -> list[ScoredTicket]:
        """Execute GraphQL query against Linear API.

        Tries Active Sprint first, falls back to general backlog if no
        active cycle exists.
        """
        headers = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Try active sprint first
            resp = await client.post(
                _LINEAR_API_URL,
                json={
                    "query": _QUERY_ACTIVE_TICKETS,
                    "variables": {
                        "teamId": self._team_id,
                        "limit": limit,
                    },
                },
                headers=headers,
            )
            data = resp.json()
            if resp.status_code != 200:
                errors = data.get("errors", [])
                logger.warning(
                    "Linear API error (status=%d): %s",
                    resp.status_code,
                    errors,
                )
                resp.raise_for_status()

        issues = data.get("data", {}).get("issues", {}).get("nodes", [])

        # Fallback to general backlog if active sprint is empty
        if not issues:
            logger.info("No active sprint tickets found, trying general backlog")
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    _LINEAR_API_URL,
                    json={
                        "query": _QUERY_BACKLOG_TICKETS,
                        "variables": {
                            "teamId": self._team_id,
                            "limit": limit,
                        },
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
            issues = data.get("data", {}).get("issues", {}).get("nodes", [])

        tickets: list[ScoredTicket] = []
        for issue in issues:
            priority = issue.get("priority", 0) or 0
            labels = tuple(
                label["name"] for label in (issue.get("labels", {}).get("nodes", []))
            )
            identifier = issue["identifier"]

            children = issue.get("children", {}).get("nodes", []) or []
            if children:
                logger.info(
                    "build_loop_filter: skipping %s (%s) — "
                    "epic/parent, not a leaf task (child_count=%d)",
                    identifier,
                    issue.get("title", ""),
                    len(children),
                )
                continue

            if any(label.lower() in _EPIC_LABEL_NAMES for label in labels):
                logger.info(
                    "build_loop_filter: skipping %s (%s) — "
                    "epic/parent label, not a leaf task",
                    identifier,
                    issue.get("title", ""),
                )
                continue

            tickets.append(
                ScoredTicket(
                    ticket_id=identifier,
                    title=issue.get("title", ""),
                    rsd_score=_PRIORITY_TO_RSD.get(priority, 0.5),
                    priority=priority,
                    labels=labels,
                    description=issue.get("description", "") or "",
                    state=issue.get("state", {}).get("name", ""),
                )
            )

        return tickets


__all__: list[str] = ["AdapterLinearFill"]
