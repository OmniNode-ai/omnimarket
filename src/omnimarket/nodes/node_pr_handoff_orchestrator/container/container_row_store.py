# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the PR handoff orchestrator's process-local row store (OMN-21623)."""

from __future__ import annotations

from omnimarket.nodes.node_pr_handoff_orchestrator.orchestration.row_store import (
    InMemoryPrHandoffRowStore,
)


def bind_process_row_store() -> InMemoryPrHandoffRowStore:
    """Bind the row store that holds the rows until the contract declares state_io (OMN-20638)."""
    return InMemoryPrHandoffRowStore()
