"""HandlerCreateTicket — ticket creation with seam detection and validation."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

# Seam signal keyword sets
_SEAM_TOPICS = {"kafka", "topic", "consumer", "producer", "event bus", "redpanda"}
_SEAM_API = {"api", "endpoint", "rest", "graphql", "webhook", "http"}
_SEAM_DB = {"database", "postgres", "migration", "schema", "table", "sql"}
_SEAM_INFRA = {"docker", "deploy", "k8s", "kubernetes", "infra", "compose"}

_PARENT_RE = re.compile(r"^OMN-\d+$")


class ModelCreateTicketRequest(BaseModel):
    """Request to create a Linear ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(..., description="Ticket title.")
    description: str = Field(default="", description="Optional description.")
    repo: str | None = Field(default=None, description="Primary repo for scoping.")
    parent: str | None = Field(default=None, description="Parent ticket ID (OMN-XXXX).")
    blocked_by: list[str] = Field(
        default_factory=list, description="Blocking ticket IDs."
    )
    dry_run: bool = Field(default=False)
    team: str = Field(default="Omninode")


class ModelCreateTicketResult(BaseModel):
    """Result of a ticket creation request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="created")
    title: str = Field(default="")
    team: str = Field(default="Omninode")
    is_seam_ticket: bool = Field(default=False)
    interfaces_touched: list[str] = Field(default_factory=list)
    contract_completeness: str = Field(default="stub")
    validation_errors: list[str] = Field(default_factory=list)
    description_body: str = Field(default="")
    dry_run: bool = Field(default=False)


def _detect_seam(title: str, description: str) -> tuple[bool, list[str]]:
    """Detect seam signals and return (is_seam, interfaces_touched)."""
    text = (title + " " + description).lower()
    interfaces: list[str] = []
    if any(kw in text for kw in _SEAM_TOPICS):
        interfaces.append("topics")
    if any(kw in text for kw in _SEAM_API):
        interfaces.append("public_api")
    if any(kw in text for kw in _SEAM_DB):
        interfaces.append("database")
    if any(kw in text for kw in _SEAM_INFRA):
        interfaces.append("infrastructure")
    return bool(interfaces), interfaces


def _generate_description_body(request: ModelCreateTicketRequest) -> str:
    """Generate a structured description body."""
    lines: list[str] = ["## Summary", ""]
    if request.description:
        lines.append(request.description)
    else:
        lines.append(f"Implement: {request.title}")
    lines.append("")
    lines.append("## Definition of Done")
    lines.append("")
    lines.append("- [ ] Implementation complete")
    lines.append("- [ ] Tests pass")
    lines.append("- [ ] PR merged")
    if request.repo:
        lines.append(f"- [ ] Verified in `{request.repo}`")
    return "\n".join(lines)


class HandlerCreateTicket:
    """Handler for ticket creation — validates input, detects seams, generates description."""

    def handle(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult:
        """Process a ticket creation request."""
        errors: list[str] = []

        # Validate parent ID format
        if request.parent and not _PARENT_RE.match(request.parent):
            errors.append(
                f"Invalid parent ID format: {request.parent!r} (expected OMN-XXXX)"
            )

        # Validate blocked_by IDs
        for bid in request.blocked_by:
            if not _PARENT_RE.match(bid):
                errors.append(
                    f"Invalid blocked_by ID format: {bid!r} (expected OMN-XXXX)"
                )

        if errors:
            return ModelCreateTicketResult(
                status="error",
                title=request.title,
                team=request.team,
                validation_errors=errors,
                dry_run=request.dry_run,
            )

        if request.dry_run:
            return ModelCreateTicketResult(
                status="dry_run",
                title=request.title,
                team=request.team,
                dry_run=True,
            )

        is_seam, interfaces = _detect_seam(request.title, request.description)
        contract_completeness = "full" if is_seam else "stub"
        description_body = _generate_description_body(request)

        return ModelCreateTicketResult(
            status="created",
            title=request.title,
            team=request.team,
            is_seam_ticket=is_seam,
            interfaces_touched=interfaces,
            contract_completeness=contract_completeness,
            description_body=description_body,
            dry_run=False,
        )


__all__: list[str] = [
    "HandlerCreateTicket",
    "ModelCreateTicketRequest",
    "ModelCreateTicketResult",
]
