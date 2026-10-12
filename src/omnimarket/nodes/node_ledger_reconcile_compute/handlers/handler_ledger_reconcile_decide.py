# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide a reconciliation from the ledger as read and the facts verified so far (OMN-20677)."""

from __future__ import annotations

from omnimarket.models.ledger_reconcile import (
    ModelPlannedAppend,
    ModelReconcileDecision,
)
from omnimarket.models.ledger_reconcile.model_ledger_reconcile_ops import (
    ModelReconcileDecideRequest,
)

from .decision import run_pipeline
from .ledger_rows import ReconcileError


class HandlerLedgerReconcileDecide:
    """Definition B: one typed request, one typed decision.

    A decision missing facts is ``needs-evidence`` and names them; the caller
    gathers exactly those and asks again with the same request plus the facts.
    """

    def handle(self, request: ModelReconcileDecideRequest) -> ModelReconcileDecision:
        try:
            pipeline = run_pipeline(request)
        except ReconcileError as exc:
            return ModelReconcileDecision(
                correlation_id=request.correlation_id,
                status="blocked",
                blocked_reason=str(exc),
            )
        if not pipeline.wanted.is_empty():
            return ModelReconcileDecision(
                correlation_id=request.correlation_id,
                status="needs-evidence",
                wanted=pipeline.wanted,
            )
        return ModelReconcileDecision(
            correlation_id=request.correlation_id,
            status="decided",
            planned=tuple(
                ModelPlannedAppend(index=p.index, kind=p.kind, row=p.row)
                for p in pipeline.planned
            ),
            refused_by_cap=pipeline.refused_by_cap,
        )
