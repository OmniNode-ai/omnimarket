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
    InMemoryPrHandoffRowStore,
    StateIoPrHandoffRowStore,
    decode_row,
    encode_row,
)

_STATE_IO_STORE = StateIoPrHandoffRowStore()
# The rows' home until the contract declares state_io (OMN-20638): the runtime
# builds one handler instance per route, so the rows live beside the seam they
# stand in for, one store per process, and a request and the observations of its
# PR meet in the same row. The state_io table replaces it; nothing else reads it.
_PROCESS_ROW_STORE = InMemoryPrHandoffRowStore()


def shared_state_io_store() -> StateIoPrHandoffRowStore:
    """The one store the handler writes to and the codec flushes from."""
    return _STATE_IO_STORE


def process_row_store() -> InMemoryPrHandoffRowStore:
    """The process's rows while no state_io table holds them (OMN-20638)."""
    return _PROCESS_ROW_STORE


class StateIoCodec:
    """encode, decode and the post-handle flush bridge."""

    def encode(self, state: ModelPrHandoffWorkflowRow) -> bytes:
        return encode_row(state).encode("utf-8")

    def decode(self, raw: bytes | str) -> ModelPrHandoffWorkflowRow:
        return decode_row(raw)

    def flush(self, cid: str) -> str | None:
        """The row the handler wrote for ``cid`` this dispatch, or None."""
        return _STATE_IO_STORE.flush(cid)


__all__: list[str] = ["StateIoCodec", "process_row_store", "shared_state_io_store"]
