# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_dirty_canonical_sweep.handlers import (
    HandlerDirtyCanonicalSweep,
)


def build_handler_dirty_canonical_sweep() -> HandlerDirtyCanonicalSweep:
    """Build the handler with its constructor defaults."""
    return HandlerDirtyCanonicalSweep()
