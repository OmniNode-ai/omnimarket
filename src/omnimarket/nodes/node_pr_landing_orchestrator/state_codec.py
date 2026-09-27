# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract-declared ``state_io`` codec of node_pr_landing_orchestrator.

omnibase_infra's state_io wiring resolves this class from the contract's
``state_io.codec`` block, loads the row keyed ``landing_key`` before
``handle()``, and after it calls :meth:`StateIoCodec.flush` to read back the
row the handler wrote, which it persists under its own compare-and-set (the
delegation orchestrator's pattern, OMN-14208). A lost race re-runs the leg.
"""

from __future__ import annotations

from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_workflow_row import (
    ModelPrLandingWorkflowRow,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    StateIoPrLandingRowStore,
    decode_row,
    encode_row,
)

_STATE_IO_STORE = StateIoPrLandingRowStore()


def shared_state_io_store() -> StateIoPrLandingRowStore:
    """The one store the handler writes to and the codec flushes from."""
    return _STATE_IO_STORE


class StateIoCodec:
    """encode, decode and the post-handle flush bridge."""

    def encode(self, state: ModelPrLandingWorkflowRow) -> bytes:
        return encode_row(state).encode("utf-8")

    def decode(self, raw: bytes | str) -> ModelPrLandingWorkflowRow:
        return decode_row(raw)

    def flush(self, cid: str) -> str | None:
        """The row the handler wrote for ``cid`` this dispatch, or None."""
        return _STATE_IO_STORE.flush(cid)


__all__: list[str] = ["StateIoCodec", "shared_state_io_store"]
