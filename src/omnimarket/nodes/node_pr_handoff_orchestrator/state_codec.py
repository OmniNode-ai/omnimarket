# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract-declared ``state_io`` codec of node_pr_handoff_orchestrator (OMN-20636).

omnibase_infra's state_io wiring resolves this class from the contract's
``state_io.codec`` block, loads the row keyed ``handoff_key`` before
``handle()``, and after it calls :meth:`StateIoCodec.flush` to read back the
row the handler wrote, which it persists under its own compare-and-set (the
landing orchestrator's pattern). A lost race re-runs the leg.
"""

from __future__ import annotations

from omnimarket.nodes.node_pr_handoff_orchestrator.models.model_pr_handoff_workflow_row import (
    ModelPrHandoffWorkflowRow,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.orchestration.row_store import (
    StateIoPrHandoffRowStore,
    decode_row,
    encode_row,
)

_STATE_IO_STORE = StateIoPrHandoffRowStore()


def shared_state_io_store() -> StateIoPrHandoffRowStore:
    """The one store the handler writes to and the codec flushes from."""
    return _STATE_IO_STORE


class StateIoCodec:
    """encode, decode and the post-handle flush bridge."""

    def encode(self, state: ModelPrHandoffWorkflowRow) -> bytes:
        return encode_row(state).encode("utf-8")

    def decode(self, raw: bytes | str) -> ModelPrHandoffWorkflowRow:
        return decode_row(raw)

    def flush(self, cid: str) -> str | None:
        """The row the handler wrote for ``cid`` this dispatch, or None."""
        return _STATE_IO_STORE.flush(cid)


__all__: list[str] = ["StateIoCodec", "shared_state_io_store"]
