# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep the receipt a local Done gate run published, for the hosted verifier (OMN-26725).

A local_done_gate run's verdict carries its receipt on the node's terminal
topic. This handler subscribes to that topic and appends the receipt, when there
is one, to the store a hosted run reads. A verdict without a receipt (a hosted
run, or a run that resolved no contract revision) stores nothing.
"""

from __future__ import annotations

from omnimarket.models.model_dod_verify_state import ModelDodVerifyState
from omnimarket.nodes.node_dod_verify.services.local_done_gate_receipts import (
    FilesystemLocalDoneGateReceiptStore,
    default_receipt_store,
)


class HandlerLocalDoneGateReceiptStore:
    """Persist a local Done gate receipt read off the terminal topic."""

    def __init__(
        self, *, store: FilesystemLocalDoneGateReceiptStore | None = None
    ) -> None:
        self._store = store

    def handle(self, verdict: ModelDodVerifyState) -> bool:
        """Return True when a receipt was stored."""
        receipt = verdict.local_done_gate_receipt
        if receipt is None:
            return False
        store = self._store if self._store is not None else default_receipt_store()
        if store is None:
            return False
        store.append(receipt)
        return True


__all__: list[str] = ["HandlerLocalDoneGateReceiptStore"]
