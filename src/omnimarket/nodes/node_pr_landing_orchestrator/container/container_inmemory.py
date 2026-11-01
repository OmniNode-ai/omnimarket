# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the in-process row store the landing orchestrator runs on when none is injected (OMN-21695).

The handler never constructs the store itself: it takes the one this module binds.
"""

from __future__ import annotations

from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)


def bind_row_store() -> InMemoryPrLandingRowStore:
    """Bind a fresh in-process row store."""
    return InMemoryPrLandingRowStore()
