# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_commitment_ledger_compute: write an operator's ask as a commitment row, and fold commitment rows.

Definition-B: ``handle(ModelCommitmentLedgerRequest) -> ModelCommitmentLedgerResult``. Pure: the clock reading, the rules
and the ledger rows come in on the request.
"""

from __future__ import annotations

from omnimarket.nodes.node_commitment_ledger_compute.handlers.commitment_rows import (
    fold_commitments,
    open_row,
    open_row_refusals,
    withdraw_row,
    withdraw_row_refusals,
)
from omnimarket.nodes.node_commitment_ledger_compute.models import (
    ModelCommitmentLedgerRequest,
    ModelCommitmentLedgerResult,
)


class HandlerCommitmentLedger:
    """Build the row that records an ask, or fold ledger rows into the commitments they declare."""

    def handle(
        self, request: ModelCommitmentLedgerRequest
    ) -> ModelCommitmentLedgerResult:
        if request.operation == "build_open_row":
            refusals = open_row_refusals(
                request.rules,
                request.lane,
                request.ask,
                request.owner,
                request.check,
                request.requires,
            )
            if refusals:
                return ModelCommitmentLedgerResult(
                    operation=request.operation, refusals=tuple(refusals)
                )
            return ModelCommitmentLedgerResult(
                operation=request.operation,
                row=open_row(
                    request.rules,
                    request.now,
                    request.lane,
                    request.ask,
                    request.owner,
                    request.check,
                    request.ticket,
                    request.requires,
                ),
            )
        if request.operation == "build_withdraw_row":
            refusals = withdraw_row_refusals(request.lane, request.commitment_id)
            if refusals:
                return ModelCommitmentLedgerResult(
                    operation=request.operation, refusals=tuple(refusals)
                )
            return ModelCommitmentLedgerResult(
                operation=request.operation,
                row=withdraw_row(
                    request.rules, request.now, request.lane, request.commitment_id
                ),
            )
        commitments, malformed = fold_commitments(request.ledger_rows, request.rules)
        return ModelCommitmentLedgerResult(
            operation=request.operation,
            commitments=tuple(commitments),
            malformed=tuple(malformed),
        )


__all__ = ["HandlerCommitmentLedger"]
