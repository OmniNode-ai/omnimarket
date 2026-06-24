"""HandlerCreateFollowupTicketsEffect.

Single-purpose side effect: structured review findings → Linear tickets.
Defaults to deterministic dry-run planning unless an adapter is injected.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from omnimarket.nodes.node_create_followup_tickets_effect.models.model_create_followup_tickets_state import (
    EnumFindingSeverity,
    ModelCreatedTicketRef,
    ModelCreateFollowupTicketsCommand,
    ModelCreateFollowupTicketsResult,
    ModelReviewFinding,
    ModelTicketCreationFailure,
)

_PRIORITY_BY_SEVERITY = {
    EnumFindingSeverity.CRITICAL: 1,
    EnumFindingSeverity.MAJOR: 2,
    EnumFindingSeverity.MINOR: 3,
    EnumFindingSeverity.NIT: 4,
}


class ProtocolFollowupTicketAdapter(Protocol):
    """Adapter boundary for Linear ticket creation."""

    def create_ticket(self, payload: dict[str, Any]) -> dict[str, str]: ...


class HandlerCreateFollowupTicketsEffect:
    """Effect handler that converts review findings into Linear tickets.

    Live mutation requires an injected adapter. Dry-run mode returns stable
    preview references and never calls external services.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(self, adapter: ProtocolFollowupTicketAdapter | None = None) -> None:
        self._adapter = adapter

    def handle(
        self, command: ModelCreateFollowupTicketsCommand
    ) -> ModelCreateFollowupTicketsResult:
        """Convert a batch of review findings into Linear tickets.

        Args:
            command: Batch of review findings with project/team/parent targeting.

        Returns:
            Result with created ticket IDs, URLs, and any per-finding failures.

        """
        created: list[ModelCreatedTicketRef] = []
        failures: list[ModelTicketCreationFailure] = []
        skipped_nits = 0
        skipped_duplicates = 0
        seen_titles: set[str] = set()

        for index, finding in enumerate(command.findings):
            if finding.severity is EnumFindingSeverity.NIT and not command.include_nits:
                skipped_nits += 1
                continue

            payload = _build_ticket_payload(command, finding, index)
            title = payload["title"]
            if title in seen_titles:
                skipped_duplicates += 1
                continue
            seen_titles.add(title)
            if command.dry_run:
                created.append(
                    ModelCreatedTicketRef(
                        finding_index=index,
                        ticket_id=f"DRY-RUN-{index + 1}",
                        ticket_url="",
                    )
                )
                continue

            if self._adapter is None:
                failures.append(
                    ModelTicketCreationFailure(
                        finding_index=index,
                        reason="linear adapter required when dry_run is false",
                    )
                )
                continue

            try:
                response = self._adapter.create_ticket(payload)
            except Exception as exc:  # pragma: no cover - adapter-specific failure
                failures.append(
                    ModelTicketCreationFailure(
                        finding_index=index,
                        reason=str(exc),
                    )
                )
                continue

            created.append(
                ModelCreatedTicketRef(
                    finding_index=index,
                    ticket_id=response["ticket_id"],
                    ticket_url=response.get("ticket_url", ""),
                )
            )

        status = _status(
            dry_run=command.dry_run,
            created_count=len(created),
            failure_count=len(failures),
        )
        return ModelCreateFollowupTicketsResult(
            status=status,
            correlation_id=command.correlation_id,
            created_tickets=tuple(created),
            failures=tuple(failures),
            skipped_nit_count=skipped_nits,
            skipped_duplicate_count=skipped_duplicates,
            dry_run=command.dry_run,
        )


def _build_ticket_payload(
    command: ModelCreateFollowupTicketsCommand,
    finding: ModelReviewFinding,
    finding_index: int,
) -> dict[str, Any]:
    location = ""
    if finding.file_path:
        location = finding.file_path
        if finding.line_number is not None:
            location = f"{location}:{finding.line_number}"

    title_bits = [finding.severity.value.upper(), finding.description.strip()]
    title = ": ".join(bit for bit in title_bits if bit)
    description_lines = [finding.description.strip()]
    if location:
        description_lines.append(f"Location: {location}")
    if finding.keyword:
        description_lines.append(f"Keyword: {finding.keyword}")
    if command.source_review_id:
        description_lines.append(f"Source review: {command.source_review_id}")

    labels = [label for label in (command.repo, finding.keyword) if label]
    return {
        "title": title,
        "description": "\n".join(description_lines),
        "priority": _PRIORITY_BY_SEVERITY[finding.severity],
        "team": command.team,
        "project": command.project,
        "parent": command.parent,
        "labels": labels,
        "finding_index": finding_index,
        "correlation_id": command.correlation_id,
    }


def _status(*, dry_run: bool, created_count: int, failure_count: int) -> str:
    if dry_run:
        return "dry_run"
    if failure_count and created_count:
        return "partial"
    if failure_count:
        return "error"
    return "created"


__all__: list[str] = [
    "HandlerCreateFollowupTicketsEffect",
    "ProtocolFollowupTicketAdapter",
]
