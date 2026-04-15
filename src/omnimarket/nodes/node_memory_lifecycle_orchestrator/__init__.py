# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Memory Lifecycle Orchestrator - ONEX Node (Core 8 Foundation).

Manages memory lifecycle transitions: ACTIVE -> STALE -> EXPIRED -> ARCHIVED -> DELETED.
Handles TTL expiration via RuntimeTick events and optimistic locking
for concurrent safety.

Node Type: ORCHESTRATOR
- Workflow coordination for memory lifecycle state transitions
- TTL expiration evaluation triggered by RuntimeTick events
- Explicit archival, expiration, and restoration commands
- Access tracking for TTL extension and pattern analysis

Time Injection:
    The orchestrator receives deterministic timestamps from RuntimeTick events
    via the `now` parameter. All timeout evaluation uses injected time rather
    than system clock, enabling deterministic testing and consistent behavior
    across distributed deployments.

Lifecycle States (EnumLifecycleState):
    - ACTIVE: Memory is available for retrieval and actively used
    - STALE: Memory is outdated but still accessible (soft TTL exceeded)
    - EXPIRED: Memory has exceeded TTL, pending cleanup
    - ARCHIVED: Memory has been moved to cold storage
    - DELETED: Memory has been permanently removed (terminal state)

ONEX 4.0 Declarative Pattern:
    This node follows the fully declarative ONEX pattern:
    - contract.yaml defines the node type, inputs, outputs, and dependencies
    - Business logic lives in handlers (HandlerMemoryTick, HandlerMemoryArchive, etc.)
    - No node.py class needed - the contract IS the node definition

Handlers::

    from omnimarket.nodes.node_memory_lifecycle_orchestrator import (
        HandlerMemoryTick,
        HandlerMemoryArchive,
        HandlerMemoryExpire,
    )

    # Planned handlers (not yet implemented):
    # HandlerRestoreMemory, HandlerMemoryAccessed

Models::

    from omnimarket.nodes.node_memory_lifecycle_orchestrator import (
        ModelArchiveMemoryCommand,
        ModelExpireMemoryCommand,
        ModelMemoryArchiveResult,
        ModelMemoryExpireResult,
        ModelMemoryTickResult,
    )

.. versionadded:: 0.1.0
    Initial implementation for OMN-1453.

Ticket: OMN-1453
"""

# Implemented handler imports
from .handlers import (
    HandlerMemoryArchive,
    HandlerMemoryExpire,
    HandlerMemoryTick,
    ModelArchiveMemoryCommand,
    ModelArchiveRecord,
    ModelExpireMemoryCommand,
    ModelMemoryArchiveResult,
    ModelMemoryCurrentState,
    ModelMemoryExpireResult,
    ModelMemoryTickResult,
    ProtocolOrphanedArchiveTracker,
)

__all__: list[str] = [
    "HandlerMemoryArchive",
    "HandlerMemoryExpire",
    "HandlerMemoryTick",
    "ModelArchiveMemoryCommand",
    "ModelArchiveRecord",
    "ModelExpireMemoryCommand",
    "ModelMemoryArchiveResult",
    "ModelMemoryCurrentState",
    "ModelMemoryExpireResult",
    "ModelMemoryTickResult",
    "ProtocolOrphanedArchiveTracker",
]
