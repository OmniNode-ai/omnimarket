# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract-declared ``state_io`` codec of node_branch_deploy_orchestrator.

omnibase_infra's state_io wiring resolves this class from the contract's
``state_io.codec`` block, loads the row keyed by the deploy's correlation id
before ``handle()``, and after it calls :meth:`StateIoCodec.flush` to read back
the row the handler wrote, which it persists under its own compare-and-set. A
lost race re-runs the leg.
"""

from __future__ import annotations

from omnimarket.nodes.node_branch_deploy_orchestrator.models.model_branch_deploy_orchestrator import (
    ModelBranchDeployContext,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.orchestration.row_store import (
    StateIoBranchDeployRowStore,
    decode_row,
    encode_row,
)

_STATE_IO_STORE = StateIoBranchDeployRowStore()


def shared_state_io_store() -> StateIoBranchDeployRowStore:
    """The one store the handler writes to and the codec flushes from."""
    return _STATE_IO_STORE


class StateIoCodec:
    """encode, decode and the post-handle flush bridge."""

    def encode(self, state: ModelBranchDeployContext) -> bytes:
        return encode_row(state).encode("utf-8")

    def decode(self, raw: bytes | str) -> ModelBranchDeployContext:
        return decode_row(raw)

    def flush(self, cid: str) -> str | None:
        """The row the handler wrote for ``cid`` this dispatch, or None."""
        return _STATE_IO_STORE.flush(cid)


__all__: list[str] = ["StateIoCodec", "shared_state_io_store"]
