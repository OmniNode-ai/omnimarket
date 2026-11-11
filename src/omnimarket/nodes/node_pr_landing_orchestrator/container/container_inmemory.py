# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the in-process row store and reducer the landing orchestrator runs on when none is injected (OMN-21695, OMN-23600).

The handler never constructs the store itself: it takes the one this module binds.
"""

from __future__ import annotations

from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer import (
    HandlerPrLandingReducer,
)


def bind_row_store() -> InMemoryPrLandingRowStore:
    """Bind a fresh in-process row store."""
    return InMemoryPrLandingRowStore()


def bind_reducer() -> HandlerPrLandingReducer:
    """Bind the pure node_pr_landing_reducer handler; the orchestrator handler never builds it itself (OMN-23600)."""
    return HandlerPrLandingReducer()
