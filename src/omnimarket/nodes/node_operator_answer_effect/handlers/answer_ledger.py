# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drain answer rows through the existing durable work-ledger emitter."""

from typing import Protocol

from omnimarket.models.operator_answer import ModelAnswerRow
from omnimarket.nodes.node_work_ledger_emit_effect import (
    HandlerWorkLedgerEmit,
    ModelWorkLedgerEmitRequest,
    ModelWorkLedgerEmitResult,
)


class ProtocolLedgerEmit(Protocol):
    def handle(
        self, request: ModelWorkLedgerEmitRequest
    ) -> ModelWorkLedgerEmitResult: ...


class WorkLedgerAnswerAppender:
    def __init__(
        self, *, ledger_id: str, emitter: ProtocolLedgerEmit | None = None
    ) -> None:
        self._ledger_id = ledger_id
        self._emitter = emitter if emitter is not None else HandlerWorkLedgerEmit()

    def append(self, row: ModelAnswerRow) -> None:
        result = self._emitter.handle(
            ModelWorkLedgerEmitRequest(
                row=row.ledger_text(),
                ledger_id=self._ledger_id,
            )
        )
        if not result.accepted:
            raise OSError(
                "answer ledger UNREAD: "
                + (result.refusal or result.error or "emit refused")
            )
