# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_dirty_canonical_sweep — auto-ship dirty canonical repo clones."""

from omnimarket.nodes.node_dirty_canonical_sweep.handlers import (
    HandlerDirtyCanonicalSweep,
)
from omnimarket.nodes.node_dirty_canonical_sweep.models import (
    ModelDirtyCanonicalSweepCommand,
    ModelDirtyCanonicalSweepResult,
    ModelDirtyRepoShipResult,
)


class NodeDirtyCanonicalSweep(HandlerDirtyCanonicalSweep):
    """ONEX entry-point wrapper for HandlerDirtyCanonicalSweep."""


__all__ = [
    "HandlerDirtyCanonicalSweep",
    "ModelDirtyCanonicalSweepCommand",
    "ModelDirtyCanonicalSweepResult",
    "ModelDirtyRepoShipResult",
    "NodeDirtyCanonicalSweep",
]
