# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""State machine validator for memory lifecycle transitions.

This module validates that lifecycle state transitions follow the defined
5-state memory lifecycle state machine.

State Machine:
    ACTIVE -> STALE -> EXPIRED -> ARCHIVED -> DELETED

    ARCHIVED -> ACTIVE is also valid, representing a memory restoration
    operation (archived memory returned to the active set).

Valid Transitions:
    ACTIVE  -> STALE    (soft TTL exceeded, still accessible)
    ACTIVE  -> EXPIRED  (hard TTL exceeded, direct expiration)
    ACTIVE  -> DELETED  (explicit delete of active memory)
    STALE   -> EXPIRED  (hard TTL exceeded after staleness)
    STALE   -> ACTIVE   (refreshed, promoted back to active)
    STALE   -> DELETED  (explicit delete of stale memory)
    EXPIRED -> ARCHIVED (moved to cold storage)
    EXPIRED -> DELETED  (explicit delete of expired memory)
    ARCHIVED -> ACTIVE  (promoted/restored from archive)
    ARCHIVED -> DELETED (explicit delete of archived memory)
    DELETED -> (none)   (terminal state - no transitions allowed)

Invalid Transitions:
    DELETED -> any      (terminal state, no recovery)
    ARCHIVED -> EXPIRED (must restore to ACTIVE first)
    ARCHIVED -> STALE   (must restore to ACTIVE first)
    EXPIRED -> ACTIVE   (must go through ARCHIVED)
    EXPIRED -> STALE    (invalid regression)

Related Tickets:
    - OMN-1603: Add adapter implementations for memory lifecycle orchestrator
    - OMN-1392: Original lifecycle orchestrator delivery

.. versionadded:: 0.1.0
    Initial implementation for OMN-1603.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from omnimemory.enums import EnumLifecycleState
from omnimemory.nodes.node_memory_lifecycle_orchestrator.validators.validator_lifecycle_transition import (
    VALID_TRANSITIONS,
    ModelTransitionValidationResult,
)

__all__ = [
    "VALID_TRANSITIONS",
    "ModelTransitionValidationResult",
    "ValidatorLifecycleTransition",
]

_VALID_TRANSITIONS = cast(
    Mapping[EnumLifecycleState, frozenset[EnumLifecycleState]],
    VALID_TRANSITIONS,
)


class ValidatorLifecycleTransition:
    """State machine validator for memory lifecycle transitions.

    Enforces the full 5-state memory lifecycle:

        ACTIVE -> STALE -> EXPIRED -> ARCHIVED -> DELETED

    With the following additional paths:
        STALE   -> ACTIVE   (soft refresh / promotion)
        ARCHIVED -> ACTIVE  (restore from archive)
        ACTIVE, STALE, EXPIRED, ARCHIVED -> DELETED (explicit deletion)

    DELETED is a terminal state with no outbound transitions.

    Usage::

        validator = ValidatorLifecycleTransition()

        result = validator.validate(
            from_state=EnumLifecycleState.ACTIVE,
            to_state=EnumLifecycleState.STALE,
        )
        assert result.valid is True

        result = validator.validate(
            from_state=EnumLifecycleState.DELETED,
            to_state=EnumLifecycleState.ACTIVE,
        )
        assert result.valid is False
        assert result.reason is not None
        assert "terminal" in result.reason

    Note:
        This validator enforces the *structural* state machine rules.
        Business logic (e.g. TTL calculation, promotion eligibility) is
        handled by the respective handlers and adapters.
    """

    def validate(
        self,
        from_state: EnumLifecycleState,
        to_state: EnumLifecycleState,
    ) -> ModelTransitionValidationResult:
        """Validate a lifecycle state transition.

        Args:
            from_state: The current lifecycle state of the memory entity.
            to_state: The desired target lifecycle state.

        Returns:
            ModelTransitionValidationResult with:
            - valid=True if the transition is allowed.
            - valid=False with a human-readable reason if the transition
              violates the state machine rules.
        """
        # Self-transitions are always invalid - a state transition must change state.
        if from_state == to_state:
            return ModelTransitionValidationResult(
                valid=False,
                from_state=from_state,
                to_state=to_state,
                reason=(
                    f"Self-transition not permitted: state is already "
                    f"'{from_state.value}'. A transition must move to a "
                    "different state."
                ),
            )

        allowed = _VALID_TRANSITIONS[from_state]

        if to_state in allowed:
            return ModelTransitionValidationResult(
                valid=True,
                from_state=from_state,
                to_state=to_state,
            )

        # Build a descriptive reason for the invalid transition.
        if from_state == EnumLifecycleState.DELETED:
            reason = (
                f"Cannot transition from '{from_state.value}': DELETED is a "
                "terminal state. No further transitions are permitted."
            )
        else:
            allowed_values = sorted(s.value for s in allowed)
            reason = (
                f"Transition '{from_state.value}' -> '{to_state.value}' is not "
                f"permitted by the lifecycle state machine. "
                f"Valid transitions from '{from_state.value}': "
                f"{allowed_values}."
            )

        return ModelTransitionValidationResult(
            valid=False,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
        )

    def is_valid(
        self,
        from_state: EnumLifecycleState,
        to_state: EnumLifecycleState,
    ) -> bool:
        """Return True if the transition is valid, False otherwise.

        Convenience wrapper around validate() for boolean checks.

        Args:
            from_state: The current lifecycle state of the memory entity.
            to_state: The desired target lifecycle state.

        Returns:
            True if the transition is allowed by the state machine.
        """
        return bool(self.validate(from_state, to_state).valid)

    def get_valid_transitions(
        self,
        from_state: EnumLifecycleState,
    ) -> frozenset[EnumLifecycleState]:
        """Return all valid destination states from a given source state.

        Args:
            from_state: The current lifecycle state.

        Returns:
            Frozenset of valid destination states. Empty if from_state
            is terminal (DELETED).
        """
        return _VALID_TRANSITIONS[from_state]
