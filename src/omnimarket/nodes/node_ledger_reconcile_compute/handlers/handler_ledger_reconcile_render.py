# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the terminal result of a reconciliation once its appends have been tried (OMN-20677)."""

from __future__ import annotations

from omnimarket.models.ledger_reconcile import ModelReconcileResult
from omnimarket.models.ledger_reconcile.model_ledger_reconcile_ops import (
    ModelReconcileRenderRequest,
)

from .decision import render_report, settle
from .evidence import COMPLETED, UNKNOWN
from .findings import ABANDONED
from .ledger_rows import ReconcileError


class HandlerLedgerReconcileRender:
    """Definition B: the decision request and the append outcomes in, the result out.

    The decision is derived again from the same inputs, so the report and the
    counts cannot disagree with what was planned.
    """

    def handle(self, request: ModelReconcileRenderRequest) -> ModelReconcileResult:
        decide = request.decide
        try:
            pipeline = settle(request)
        except ReconcileError as exc:
            error = f"ledger_reconcile: NOT RUN — {exc}\n"
            return ModelReconcileResult(
                correlation_id=decide.correlation_id,
                exit_code=3,
                status="blocked",
                stderr=error,
                notes=error.strip(),
            )
        findings = pipeline.findings
        apply = decide.params.apply
        report = render_report(findings, pipeline.parsed, apply)
        refused = [f for f in findings if f.applied == "refused-by-cap"]
        failures = [f for f in findings if f.applied.startswith("append-failed")]
        error = ""
        if refused:
            error = (
                f"ledger_reconcile: REFUSED — this pass would append {len(refused)} rows, "
                f"more than --max-appends {decide.params.max_appends}. Nothing was appended.\n"
            )
        code = 4 if refused else 2 if failures else 1 if findings else 0
        return ModelReconcileResult(
            correlation_id=decide.correlation_id,
            exit_code=code,
            stdout=report,
            stderr=error,
            status=(
                "blocked"
                if code > 1
                else "clean"
                if not findings
                else "reconciled"
                if apply
                else "report-only"
            ),
            dangling=len(findings),
            auto_closed=sum(
                f.applied == "terminal-appended" and f.verdict == COMPLETED
                for f in findings
            ),
            abandoned=sum(
                f.applied == "terminal-appended" and f.verdict == ABANDONED
                for f in findings
            ),
            released=sum(f.applied == "release-appended" for f in findings),
            needs_attention=sum(f.applied == "attention-appended" for f in findings),
            held=sum(bool(f.held) for f in findings),
            unknown=sum(f.verdict == UNKNOWN for f in findings),
            unparseable=len(pipeline.parsed.unparseable),
            notes=error.strip()
            or "\n".join(
                line
                for line in report.splitlines()
                if line.startswith(("parsed by shape:", "positive control:"))
            ),
        )
