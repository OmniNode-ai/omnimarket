# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Append the rows a decision planned, each through the ledger writer (OMN-20677)."""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.ledger_reconcile import ModelAppendOutcome
from omnimarket.models.ledger_reconcile.model_ledger_reconcile_ops import (
    ModelAppendRowsRequest,
    ModelAppendRowsResult,
)

from ..protocols import (
    ProtocolReconcileAppender,
    ProtocolReconcileHost,
    ReconcilePortError,
)
from ..protocols.local_ledger_reconcile_adapters import (
    LedgerWriterAppender,
    LocalReconcileHost,
)


class HandlerAppendRows:
    """Definition B: planned rows in, one outcome per row out.

    A row that fails does not stop the rest; each outcome carries its own error.
    The ledger writer owns the lock, the grammar and the duplicate guard.
    """

    def __init__(
        self,
        appender: ProtocolReconcileAppender | None = None,
        host: ProtocolReconcileHost | None = None,
    ) -> None:
        self._appender: ProtocolReconcileAppender = (
            appender if appender is not None else LedgerWriterAppender()
        )
        self._host: ProtocolReconcileHost = (
            host if host is not None else LocalReconcileHost()
        )

    async def handle(self, request: ModelAppendRowsRequest) -> ModelAppendRowsResult:
        try:
            ledger = (
                Path(request.ledger_path)
                if request.ledger_path
                else self._host.ledger_path()
            )
        except (ReconcilePortError, KeyError) as exc:
            return ModelAppendRowsResult(
                correlation_id=request.correlation_id,
                error=f"ledger path unreadable: {exc}",
            )
        return ModelAppendRowsResult(
            correlation_id=request.correlation_id,
            outcomes=tuple(
                ModelAppendOutcome(
                    index=row.index, error=self._appender.append(ledger, row.row)
                )
                for row in request.rows
            ),
        )
