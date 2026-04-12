# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Memory Lifecycle Orchestrator Validators.

Validation logic for lifecycle state transitions.

Validators:
    ValidatorLifecycleTransition: Validates lifecycle state machine transitions.
        Enforces the full 5-state memory lifecycle:
        ACTIVE -> STALE -> EXPIRED -> ARCHIVED -> DELETED

        With additional transitions:
        STALE   -> ACTIVE   (soft refresh / promotion)
        ARCHIVED -> ACTIVE  (restore from archive)
        ACTIVE, STALE, EXPIRED, ARCHIVED -> DELETED (explicit deletion)

Validation Rules:
    State transitions must follow the lifecycle state machine:
    - ACTIVE  -> STALE, EXPIRED, DELETED
    - STALE   -> ACTIVE, EXPIRED, DELETED
    - EXPIRED -> ARCHIVED, DELETED
    - ARCHIVED -> ACTIVE, DELETED
    - DELETED -> (terminal, no transitions)

Invalid Transitions:
    - DELETED -> any state (terminal, no recovery)
    - ARCHIVED -> EXPIRED (must restore first)
    - EXPIRED -> ACTIVE   (must archive then restore)
    - Self-transitions (same state to same state)

Related Tickets:
    - OMN-1603: Add adapter implementations for memory lifecycle orchestrator
    - OMN-1453: OmniMemory P4b - Lifecycle Orchestrator Database Integration

.. versionadded:: 0.1.0
    Initial implementation for OMN-1603.
"""

from omnimarket.nodes.node_memory_lifecycle_orchestrator.validators.validator_lifecycle_transition import (
    VALID_TRANSITIONS,
    ModelTransitionValidationResult,
    ValidatorLifecycleTransition,
)

__all__: list[str] = [
    "VALID_TRANSITIONS",
    "ModelTransitionValidationResult",
    "ValidatorLifecycleTransition",
]
