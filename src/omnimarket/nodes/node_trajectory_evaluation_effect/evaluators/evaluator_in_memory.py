# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic in-memory evaluator (OMN-20087).

The receipt id is derived from the idempotency key, a second submit of the same
key returns the same receipt with ``duplicate`` true, and a receipt turns
terminal after a configured number of status reads.
"""

from __future__ import annotations

import hashlib

from omnimarket.nodes.node_trajectory_evaluation_effect.protocols import (
    EvaluatorRejectedError,
    ModelEvaluatorReceipt,
    ModelEvaluatorStatus,
    ModelEvaluatorSubmission,
)


class EvaluatorInMemory:
    """Process-local evaluator; nothing leaves the process."""

    def __init__(self, *, terminal_after_reads: int = 1) -> None:
        self.terminal_after_reads = terminal_after_reads
        self._receipts: dict[str, str] = {}
        self._reads: dict[str, int] = {}

    @property
    def host(self) -> str:
        return "in-memory"

    async def submit(
        self, submission: ModelEvaluatorSubmission
    ) -> ModelEvaluatorReceipt:
        key = submission.idempotency_key
        existing = self._receipts.get(key)
        if existing is not None:
            return ModelEvaluatorReceipt(receipt_id=existing, duplicate=True)
        receipt_id = "mem-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        self._receipts[key] = receipt_id
        self._reads[receipt_id] = 0
        return ModelEvaluatorReceipt(receipt_id=receipt_id, duplicate=False)

    async def read_status(self, receipt_id: str) -> ModelEvaluatorStatus:
        if receipt_id not in self._reads:
            raise EvaluatorRejectedError("unknown receipt", status_code=404)
        self._reads[receipt_id] += 1
        if self._reads[receipt_id] >= self.terminal_after_reads:
            return ModelEvaluatorStatus(terminal=True, verdict="scored")
        return ModelEvaluatorStatus(terminal=False)
