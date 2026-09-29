"""Pure definition-B fold for board probe results (OMN-19937)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from omnimarket.nodes.node_projection_board_probe_results.models import (
    BoardProbeResultKey,
    EnumBoardProbeOutcome,
    ModelBoardProbeResultEvent,
    ModelBoardProbeResultPayload,
    ModelBoardProbeResultRow,
    ModelBoardProbeResultsProjectionResult,
    VerdictRequest,
)


class HandlerProjectionBoardProbeResults:
    """Fold one payload into one deterministic result row, without effects."""

    def fold(
        self,
        existing_row: ModelBoardProbeResultRow | None,
        event: ModelBoardProbeResultEvent,
    ) -> ModelBoardProbeResultRow:
        """Return the winner under ``(finished_at, source_offset)`` ordering."""
        candidate = ModelBoardProbeResultRow(
            check_id=event.check_id,
            subject_kind=event.subject_kind,
            subject=event.subject,
            repo=event.repo,
            sha=event.sha,
            surface_instance=event.surface_instance,
            execution_id=event.execution_id,
            outcome=event.outcome,
            reasons=event.reasons,
            evidence_items=event.evidence_items,
            finished_at=event.finished_at,
            source_offset=event.source_offset,
        )
        if existing_row is None:
            return candidate
        if existing_row.key != candidate.key:
            raise ValueError("existing row key differs from board probe event key")
        existing_order = (existing_row.finished_at, existing_row.source_offset)
        candidate_order = (candidate.finished_at, candidate.source_offset)
        return candidate if candidate_order > existing_order else existing_row

    def handle(
        self, request: ModelBoardProbeResultPayload
    ) -> ModelBoardProbeResultsProjectionResult:
        """Canonical definition-B entry point. Pure and envelope-free."""
        event = ModelBoardProbeResultEvent.model_validate(
            {**request.model_dump(mode="json"), "source_offset": 0}
        )
        return ModelBoardProbeResultsProjectionResult(row=self.fold(None, event))


def satisfies_verdict_request(
    rows: Iterable[ModelBoardProbeResultRow | Mapping[str, Any]],
    request: VerdictRequest,
) -> bool:
    """Return true only for an exact-key PASS, including execution identity."""
    for row in rows:
        key: BoardProbeResultKey
        outcome: str
        if isinstance(row, ModelBoardProbeResultRow):
            key = row.key
            outcome = row.outcome.value
        else:
            key = (
                str(row.get("check_id", "")),
                str(row.get("subject_kind", "")),
                str(row.get("repo", "")),
                str(row.get("sha", "")),
                str(row.get("surface_instance", "")),
                str(row.get("execution_id", "")),
            )
            outcome = str(row.get("outcome", ""))
        if key == request and outcome == EnumBoardProbeOutcome.PASS.value:
            return True
    return False


__all__ = [
    "HandlerProjectionBoardProbeResults",
    "satisfies_verdict_request",
]
