# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Append the rows a decision planned, each over the bus append path (OMN-20677)."""

from __future__ import annotations

from omnimarket.models.ledger_reconcile import ModelAppendOutcome
from omnimarket.models.ledger_reconcile.model_ledger_reconcile_ops import (
    ModelAppendRowsRequest,
    ModelAppendRowsResult,
)

from ..protocols import ProtocolReconcileAppender
from ..protocols.local_ledger_reconcile_adapters import BusLedgerAppender


class HandlerAppendRows:
    """Definition B: planned rows in, one outcome per row out.

    A row that fails does not stop the rest; each outcome carries its own error.
    The append node owns the grammar and the duplicate guard; a row is sent under a request id derived from it.
    """

    def __init__(self, appender: ProtocolReconcileAppender | None = None) -> None:
        self._appender: ProtocolReconcileAppender = (
            appender if appender is not None else BusLedgerAppender()
        )

    async def handle(self, request: ModelAppendRowsRequest) -> ModelAppendRowsResult:
        return ModelAppendRowsResult(
            correlation_id=request.correlation_id,
            outcomes=tuple(
                ModelAppendOutcome(
                    index=row.index, error=self._appender.append(row.row)
                )
                for row in request.rows
            ),
        )
