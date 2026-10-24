# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure, deterministic fold of install-confirm results into boundary receipts.

The selected activation owns a custom boundary's verdict. Other activations
marked not_selected at that boundary cannot overwrite it. Native and unreadable
boundaries still get a receipt when no custom activation was published.
Decided receipts are immutable; a fuller capture may resolve an uncertain one.
"""

from __future__ import annotations

from omnimarket.models.context_install_confirm import (
    EnumBoundaryKind,
    EnumInstallFact,
    EnumInstallFactReason,
    ModelBoundaryReading,
    ModelInstallationFact,
)
from omnimarket.nodes.node_projection_compaction_receipts.models import (
    ModelCompactionReceipt,
    ModelCompactionReceiptsProjectionRequest,
    ModelCompactionReceiptsProjectionResult,
)


def _boundary_receipt(
    session_id: str,
    boundary: ModelBoundaryReading,
    facts: tuple[ModelInstallationFact, ...],
) -> ModelCompactionReceipt:
    selected = next(
        (
            fact
            for fact in facts
            if fact.activation_id == boundary.activation_id
            and fact.boundary_uuid == boundary.boundary_uuid
        ),
        None,
    )
    if selected is not None:
        verdict, reason = selected.fact, selected.reason
    elif boundary.kind is EnumBoundaryKind.NATIVE:
        verdict, reason = (
            EnumInstallFact.NOT_INSTALLED,
            EnumInstallFactReason.NATIVE_COMPACTION,
        )
    else:
        # A named activation missing from the capture cannot prove installation.
        verdict = EnumInstallFact.UNCERTAIN
        reason = (
            EnumInstallFactReason.UNPARSEABLE_RECORD
            if boundary.kind is EnumBoundaryKind.UNREADABLE
            or boundary.observed_hash is None
            else EnumInstallFactReason.CAPTURE_GAP
        )
    return ModelCompactionReceipt(
        session_id=session_id,
        receipt_key=f"boundary:{boundary.boundary_uuid}",
        boundary_uuid=boundary.boundary_uuid,
        boundary_kind=boundary.kind,
        activation_id=boundary.activation_id,
        verdict=verdict,
        reason=reason,
        installed_messages_hash=selected.installed_messages_hash if selected else None,
        observed_hash=boundary.observed_hash,
    )


class HandlerProjectionCompactionReceipts:
    """Materialize one row per captured compaction, independent of delivery order."""

    def handle(
        self, request: ModelCompactionReceiptsProjectionRequest
    ) -> ModelCompactionReceiptsProjectionResult:
        event = request.event
        boundaries = {boundary.boundary_uuid: boundary for boundary in event.boundaries}

        rows = [
            _boundary_receipt(event.session_id, boundary, event.facts)
            for boundary in boundaries.values()
        ]
        rows.extend(
            ModelCompactionReceipt(
                session_id=event.session_id,
                receipt_key=f"activation:{fact.activation_id}",
                boundary_uuid=None,
                boundary_kind=None,
                activation_id=fact.activation_id,
                verdict=fact.fact,
                reason=fact.reason,
                installed_messages_hash=fact.installed_messages_hash,
                observed_hash=fact.observed_hash,
            )
            for fact in event.facts
            if fact.boundary_uuid is None
        )
        previous = {
            (row.session_id, row.receipt_key): row for row in request.previous_rows
        }
        return ModelCompactionReceiptsProjectionResult(
            rows=tuple(
                row
                for row in sorted(rows, key=lambda row: row.receipt_key)
                if (prior := previous.get((row.session_id, row.receipt_key))) is None
                or (
                    prior.verdict is EnumInstallFact.UNCERTAIN
                    and row.verdict is not EnumInstallFact.UNCERTAIN
                )
            )
        )


__all__ = ["HandlerProjectionCompactionReceipts"]
