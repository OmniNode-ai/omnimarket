# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTicketProject: clear a project Linear copied from a parent, and repair the ones missed.

Every new ticket is created in the Backlog with no project (operator ruling 2026-09-30T14:30:05Z).
Linear copies a parent's project onto a sub-issue created without one, and nothing that sees only
the create call can notice it. The documented recovery, an update with ``project=null``, has no
spelling in the Linear MCP: its ``project`` argument is a string, so ``null`` arrives as the text
``null`` and names a project that does not exist. This handler performs the one spelling that
works, a direct GraphQL ``issueUpdate(projectId: null)``, and then reads the issue back in a
separate query: success is reported only when that readback carries an explicit null project.

Two operations of ``node_create_ticket``:

* ``clear_project`` -- clear one ticket, the step the create-ticket skill runs after its MCP create.
* ``repair_project`` -- list the lane-intake-labelled tickets of one UTC day that sit in a named
  project and clear each after rechecking its creation day, label and project.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.handler_lane_intake import (
    ModelLaneIntakePolicy,
)
from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)

_READ_QUERY = """
query GetIssueProject($id: String!) {
  issue(id: $id) { identifier createdAt labels { nodes { name } } project { name } }
}
"""

# The explicit literal null is the whole point: an omitted projectId changes nothing.
_CLEAR_MUTATION = """
mutation ClearIssueProject($id: String!) {
  issueUpdate(id: $id, input: { projectId: null }) { success issue { identifier project { name } } }
}
"""

_LIST_QUERY = """
query ListIssuesInProject($after: String, $from: DateTimeOrDuration!, $to: DateTimeOrDuration!, $label: String!, $project: String!) {
  issues(first: 100, after: $after, filter: { createdAt: { gte: $from, lt: $to }, labels: { name: { eq: $label } }, project: { name: { eq: $project } } }) {
    pageInfo { hasNextPage endCursor }
    nodes { identifier }
  }
}
"""

_MAX_PAGES = 50


class TicketProjectNotClearedError(RuntimeError):
    """The independent readback after a clear did not show an explicit null project."""


class ModelTicketProjectReading(BaseModel):
    """One independent read of a ticket's project, creation time and labels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identifier: str
    created_at: str
    labels: list[str] = Field(default_factory=list)
    project: str | None = Field(
        default=None, description="The project name, or None for an explicit null."
    )
    project_reported: bool = Field(
        description="False when the response carried no project key at all, which is not a null."
    )


class TicketProjectGatewayProtocol(Protocol):
    """The Linear reads and the one write the project handler uses."""

    def read(self, ticket_id: str) -> ModelTicketProjectReading: ...

    def clear(self, ticket_id: str) -> None: ...

    def list_candidates(
        self, *, day_start: str, day_end: str, label: str, project: str
    ) -> list[str]: ...


class LinearTicketProjectGateway:
    """The project gateway over a ``post(query, variables)`` GraphQL seam."""

    def __init__(self, post: Callable[[str, dict[str, Any]], Any]) -> None:
        self._post = post

    def read(self, ticket_id: str) -> ModelTicketProjectReading:
        data = self._post(_READ_QUERY, {"id": ticket_id})
        issue = (data.get("data") or {}).get("issue")
        if not isinstance(issue, dict):
            raise RuntimeError(f"Linear returned no issue for {ticket_id}")
        project = issue.get("project")
        labels = (issue.get("labels") or {}).get("nodes") or []
        return ModelTicketProjectReading(
            identifier=str(issue.get("identifier", "")),
            created_at=str(issue.get("createdAt", "")),
            labels=[str(label.get("name", "")) for label in labels],
            project=str(project.get("name", "")) if isinstance(project, dict) else None,
            project_reported="project" in issue,
        )

    def clear(self, ticket_id: str) -> None:
        data = self._post(_CLEAR_MUTATION, {"id": ticket_id})
        update = (data.get("data") or {}).get("issueUpdate") or {}
        if update.get("success") is not True:
            raise RuntimeError(
                f"Linear did not accept the project clear for {ticket_id}"
            )

    def list_candidates(
        self, *, day_start: str, day_end: str, label: str, project: str
    ) -> list[str]:
        found: list[str] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            data = self._post(
                _LIST_QUERY,
                {
                    "after": after,
                    "from": day_start,
                    "to": day_end,
                    "label": label,
                    "project": project,
                },
            )
            issues = (data.get("data") or {}).get("issues") or {}
            found += [str(n["identifier"]) for n in issues.get("nodes") or []]
            page = issues.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return found
            after = page.get("endCursor")
        raise RuntimeError(f"Linear listing did not end within {_MAX_PAGES} pages")


def clear_inherited_project(
    gateway: TicketProjectGatewayProtocol, ticket_id: str
) -> bool:
    """Clear ``ticket_id``'s project if it has one; return True when an update was sent.

    A ticket that reads back with no project sends no update. After an update the ticket is read
    again, and anything but an explicit null project raises :class:`TicketProjectNotClearedError`.
    """
    before = gateway.read(ticket_id)
    if before.project_reported and before.project is None:
        return False
    gateway.clear(ticket_id)
    _require_explicit_null(gateway.read(ticket_id), ticket_id, before.project)
    return True


def _require_explicit_null(
    after: ModelTicketProjectReading, ticket_id: str, was: str | None
) -> None:
    if not after.project_reported:
        raise TicketProjectNotClearedError(
            f"{ticket_id}: the readback carried no project field, so no explicit null; "
            "refusing to report the project cleared"
        )
    if after.project is not None:
        raise TicketProjectNotClearedError(
            f"{ticket_id}: the readback still shows project {after.project!r} "
            f"(was {was!r}); refusing to report the project cleared"
        )


class HandlerTicketProject:
    """clear_project and repair_project over one project gateway.

    This is the handler ``contract.yaml`` routes both operations to, so it is the one place every
    path passes: a ``dry_run`` request writes nothing here, and ``repair_project`` takes the
    lane-intake label from the policy beside the installed ticket guard unless a ``label`` or a
    ``lane_intake_policy`` was given.
    """

    def __init__(
        self,
        gateway: TicketProjectGatewayProtocol | None = None,
        label: str = "",
        *,
        lane_intake_policy: ModelLaneIntakePolicy | None = None,
    ) -> None:
        self._injectable_gateway = gateway
        self._label = label
        self._injectable_policy = lane_intake_policy

    @property
    def _gateway(self) -> TicketProjectGatewayProtocol:
        """The injected gateway, else the live one over the contract-declared Linear key."""
        if self._injectable_gateway is None:
            # Local import: handler_create_ticket imports this module.
            from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
                build_project_gateway,
            )

            self._injectable_gateway = build_project_gateway()
        return self._injectable_gateway

    def _lane_intake_label(self) -> str:
        """The lane-intake label: the one given, else the policy's, else the installed guard's."""
        if self._label:
            return self._label
        policy = self._injectable_policy
        if policy is None:
            # Local import: handler_create_ticket imports this module.
            from omnimarket.nodes.node_create_ticket.handlers.core_ticket_guard import (
                core_lane_intake_policy,
            )

            policy = core_lane_intake_policy()
        if not policy.label:
            raise RuntimeError("repair_project needs the lane-intake label")
        return policy.label

    def handle(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult:
        """Run the request's clear_project or repair_project operation."""
        if request.operation is EnumTicketOperation.CLEAR_PROJECT:
            return self.clear(str(request.ticket_id), dry_run=request.dry_run)
        if request.operation is EnumTicketOperation.REPAIR_PROJECT:
            return self.repair(
                day=request.day,
                from_project=request.from_project.strip(),
                label=self._lane_intake_label(),
                dry_run=request.dry_run,
            )
        raise ValueError(f"not a project operation: {request.operation.value}")

    def clear(
        self, ticket_id: str, *, dry_run: bool = False
    ) -> ModelCreateTicketResult:
        """Clear one ticket's project and report success only on an explicit null readback.

        A dry run returns before the gateway is touched: no read, no update.
        """
        if dry_run:
            return ModelCreateTicketResult(
                status="dry_run",
                operation=EnumTicketOperation.CLEAR_PROJECT,
                ticket_id=ticket_id,
                dry_run=True,
            )
        was = self._gateway.read(ticket_id).project
        self._gateway.clear(ticket_id)
        _require_explicit_null(self._gateway.read(ticket_id), ticket_id, was)
        return ModelCreateTicketResult(
            status="project_cleared",
            operation=EnumTicketOperation.CLEAR_PROJECT,
            ticket_id=ticket_id,
            project_cleared=True,
        )

    def repair(
        self, *, day: str, from_project: str, label: str, dry_run: bool
    ) -> ModelCreateTicketResult:
        """Clear ``from_project`` from each ``label`` ticket created on the UTC ``day``."""
        start = date.fromisoformat(day)
        day_start = f"{start.isoformat()}T00:00:00.000Z"
        day_end = f"{(start + timedelta(days=1)).isoformat()}T00:00:00.000Z"
        candidates = self._gateway.list_candidates(
            day_start=day_start, day_end=day_end, label=label, project=from_project
        )
        if dry_run:
            return ModelCreateTicketResult(
                status="dry_run",
                operation=EnumTicketOperation.REPAIR_PROJECT,
                candidate_ids=candidates,
                dry_run=True,
            )
        cleared: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for ticket_id in candidates:
            now = self._gateway.read(ticket_id)
            reason = self._recheck(now, day, label, from_project)
            if reason:
                skipped.append(f"{ticket_id}: {reason}")
                continue
            try:
                self._gateway.clear(ticket_id)
                _require_explicit_null(
                    self._gateway.read(ticket_id), ticket_id, now.project
                )
            except (RuntimeError, TicketProjectNotClearedError) as exc:
                failed.append(f"{ticket_id}: {exc}")
                continue
            cleared.append(ticket_id)
        return ModelCreateTicketResult(
            status="error" if failed else "project_repaired",
            operation=EnumTicketOperation.REPAIR_PROJECT,
            candidate_ids=candidates,
            cleared_ids=cleared,
            skipped=skipped,
            validation_errors=failed,
        )

    @staticmethod
    def _recheck(
        now: ModelTicketProjectReading,
        day: str,
        label: str,
        from_project: str,
    ) -> str:
        """Why ``ticket_id`` must not be written now, or '' when it still matches the listing."""
        if not now.created_at.startswith(f"{day}T"):
            return f"created {now.created_at!r}, not on {day}"
        if label not in now.labels:
            return f"no {label!r} label"
        if now.project != from_project:
            return f"project is {now.project!r}, not {from_project!r}"
        return ""
