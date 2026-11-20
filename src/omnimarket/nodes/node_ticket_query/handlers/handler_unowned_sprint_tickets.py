# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""list_unowned_sprint_tickets: the open tickets of the current sprint that no lane holds.

``handle(request: ModelUnownedSprintTicketsInput) -> ModelUnownedSprintTicketsOutput``. The fill
reads this instead of a hand-written sprint step: the sprint is resolved by date from Linear (never
a typed id), its open tickets (state not completed or canceled) are listed, and every ticket a live
CLAIM holds is dropped. A live CLAIM is an open CLAIM row with no later TERMINAL naming the ticket
or its lane, read through the work ledger reader node (never a direct file open). A ledger the
reader cannot answer raises: an unread claim set would read every ticket as unowned.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.handler_sprint_resolve import (
    MAX_PAGES,
    SPRINT_PROJECTS_QUERY,
    projects_from_nodes,
    resolve_sprint_for_date,
    utc_today,
)
from omnimarket.models.model_work_ledger_reader import (
    EnumReaderQuery,
    EnumReaderStatus,
    ModelReaderRequest,
)
from omnimarket.models.sprint_resolution.model_sprint_resolution import (
    ModelSprintProjectListing,
    ModelSprintResolution,
    ModelSprintResolveRequest,
)
from omnimarket.models.ticket_query.model_unowned_sprint_tickets import (
    ModelUnownedSprintTicket,
    ModelUnownedSprintTicketsInput,
    ModelUnownedSprintTicketsOutput,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_ticket_query.handlers.store_resolved_tracker import (
    ProtocolGraphQLPoster,
    resolve_linear_tracker,
)

#: The reader name the deployment's reader overlay lists in ``moved_readers``.
READER_NAME = "unowned-sprint-tickets"

_SPRINT_OPEN_ISSUES_QUERY = """
query SprintOpenIssues($project: ID!, $after: String) {
  issues(first: 100, after: $after, filter: {
    project: { id: { eq: $project } }, state: { type: { nin: ["completed", "canceled"] } }
  }) {
    pageInfo { hasNextPage endCursor }
    nodes { identifier title state { name type } assignee { id } labels { nodes { name } } }
  }
}
"""


class LedgerUnreadableError(RuntimeError):
    """The work ledger reader gave no usable CLAIM and TERMINAL rows."""


class ModelClaimRows(BaseModel):
    """The ledger reader's answer to a CLAIM and TERMINAL rows read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    lines: tuple[str, ...] = ()
    reason: str = ""


class ProtocolClaimRows(Protocol):
    """Reads the CLAIM and TERMINAL rows of the rolling ledger within a window."""

    def read_claim_rows(self, window_hours: int) -> ModelClaimRows: ...


class ReaderClaimRows:
    """CLAIM and TERMINAL rows through the work ledger reader node."""

    def __init__(self, reader: Callable[[ModelReaderRequest], object]) -> None:
        self._reader = reader

    def read_claim_rows(self, window_hours: int) -> ModelClaimRows:
        result = self._reader(
            ModelReaderRequest(
                reader=READER_NAME,
                query=EnumReaderQuery.ROWS,
                window_hours=window_hours,
                kinds=("CLAIM", "TERMINAL"),
            )
        )
        return ModelClaimRows(
            status=str(getattr(getattr(result, "status", ""), "value", "")),
            lines=tuple(getattr(result, "lines", ())),
            reason=str(getattr(result, "reason", "")),
        )


def build_ledger_claim_rows(
    environ: Mapping[str, str] | None = None,
) -> ReaderClaimRows:
    """The live reader: the deployment's reader overlay and point read, as the other readers build it."""
    from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
        InMemoryEventSink,
        build_point_read,
    )
    from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
        HandlerWorkLedgerReader,
    )
    from omnimarket.nodes.node_work_ledger_reader_effect.overlay import (
        load_overlay as load_reader_overlay,
    )

    env = os.environ if environ is None else environ
    overlay = load_reader_overlay(env)
    reader = HandlerWorkLedgerReader(
        overlay, build_point_read(overlay.route, env), InMemoryEventSink()
    )
    return ReaderClaimRows(reader.handle)


def _cells(line: str) -> tuple[str, str, dict[str, str]]:
    parts = [c.strip() for c in line.split(" | ")]
    fields: dict[str, str] = {}
    for cell in parts[2:]:
        key, sep, value = cell.partition("=")
        if sep and key and key.replace("_", "").replace("-", "").isalnum():
            fields.setdefault(key, value.strip())
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else ""), fields


def live_claimed_tickets(lines: tuple[str, ...]) -> frozenset[str]:
    """The tickets an open CLAIM holds: a CLAIM with no later TERMINAL naming its ticket or lane."""
    rows = sorted(
        (_cells(line) for line in lines if " | " in line), key=lambda row: row[0]
    )
    open_claims: list[tuple[str, str]] = []  # (ticket, lane)
    for _, kind, fields in rows:
        if kind == "CLAIM":
            open_claims.append((fields.get("ticket", ""), fields.get("lane", "")))
        elif kind == "TERMINAL":
            ticket, lane = fields.get("ticket", ""), fields.get("lane", "")
            open_claims = [
                (t, ln)
                for t, ln in open_claims
                if not ((ticket and t == ticket) or (lane and ln == lane))
            ]
    return frozenset(t for t, _ in open_claims if t)


async def _store_resolved_tracker() -> ProtocolGraphQLPoster:
    return await resolve_linear_tracker(HandlerSecretStore())


class HandlerUnownedSprintTickets:
    """EFFECT: the current sprint's open tickets with no live CLAIM."""

    def __init__(
        self,
        tracker_provider: Callable[[], Awaitable[ProtocolGraphQLPoster]] | None = None,
        claim_rows: ProtocolClaimRows | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._tracker_provider = tracker_provider or _store_resolved_tracker
        self._claim_rows = claim_rows
        self._clock = clock

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def _post(
        self, tracker: ProtocolGraphQLPoster, query: str, variables: dict[str, object]
    ) -> dict[str, object]:
        response = await tracker.post_graphql(query, variables)
        if response.status_code != 200:
            raise ConnectionError(f"tracker answered HTTP {response.status_code}")
        body = response.json()
        if body.get("errors"):
            raise ConnectionError("tracker returned GraphQL errors")
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    async def _sprint_projects(
        self, tracker: ProtocolGraphQLPoster
    ) -> list[ModelSprintProjectListing]:
        found: list[ModelSprintProjectListing] = []
        after: str | None = None
        for _ in range(MAX_PAGES):
            data = await self._post(tracker, SPRINT_PROJECTS_QUERY, {"after": after})
            projects = data.get("projects")
            projects = projects if isinstance(projects, dict) else {}
            found += projects_from_nodes(list(projects.get("nodes") or []))
            page = projects.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return found
            after = page.get("endCursor")
        raise RuntimeError("Linear sprint project listing did not end in time")

    async def _open_issues(
        self, tracker: ProtocolGraphQLPoster, project_id: str
    ) -> list[ModelUnownedSprintTicket]:
        found: list[ModelUnownedSprintTicket] = []
        after: str | None = None
        for _ in range(MAX_PAGES):
            data = await self._post(
                tracker,
                _SPRINT_OPEN_ISSUES_QUERY,
                {"project": project_id, "after": after},
            )
            issues = data.get("issues")
            issues = issues if isinstance(issues, dict) else {}
            for node in issues.get("nodes") or []:
                state = node.get("state") or {}
                labels = (node.get("labels") or {}).get("nodes") or []
                found.append(
                    ModelUnownedSprintTicket(
                        identifier=str(node["identifier"]),
                        title=str(node.get("title") or ""),
                        state_name=str(state.get("name") or ""),
                        state_type=str(state.get("type") or ""),
                        assignee_id=str((node.get("assignee") or {}).get("id") or ""),
                        labels=tuple(str(x.get("name") or "") for x in labels),
                    )
                )
            page = issues.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return found
            after = page.get("endCursor")
        raise RuntimeError("Linear sprint issue listing did not end in time")

    async def handle(
        self, request: ModelUnownedSprintTicketsInput
    ) -> ModelUnownedSprintTicketsOutput:
        tracker = await self._tracker_provider()
        sprint: ModelSprintResolution = resolve_sprint_for_date(
            ModelSprintResolveRequest(
                date=utc_today(self._clock),
                projects=tuple(await self._sprint_projects(tracker)),
            )
        )
        if not sprint.resolved:
            return ModelUnownedSprintTicketsOutput(sprint=sprint)
        open_tickets = await self._open_issues(tracker, sprint.project_id)
        rows = (self._claim_rows or build_ledger_claim_rows()).read_claim_rows(
            request.claim_window_hours
        )
        if rows.status not in (EnumReaderStatus.OK.value, EnumReaderStatus.STALE.value):
            raise LedgerUnreadableError(
                f"the work ledger reader answered status={rows.status!r} "
                f"reason={rows.reason!r}; an unread claim set would read every ticket as unowned"
            )
        held = live_claimed_tickets(rows.lines)
        return ModelUnownedSprintTicketsOutput(
            sprint=sprint,
            tickets=tuple(t for t in open_tickets if t.identifier not in held),
            open_in_sprint=len(open_tickets),
            claimed=tuple(
                sorted(t.identifier for t in open_tickets if t.identifier in held)
            ),
            ledger_status=rows.status,
        )


__all__ = [
    "READER_NAME",
    "HandlerUnownedSprintTickets",
    "LedgerUnreadableError",
    "ModelClaimRows",
    "ProtocolClaimRows",
    "ReaderClaimRows",
    "build_ledger_claim_rows",
    "live_claimed_tickets",
]
