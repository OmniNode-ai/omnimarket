# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node entry point for the capsule-effectiveness feedback edge (OMN-12845)."""

from __future__ import annotations

from omnimarket.nodes.node_capsule_effectiveness_feedback_reducer.handlers.handler_capsule_effectiveness_feedback import (
    HandlerCapsuleEffectivenessFeedback,
)


class NodeCapsuleEffectivenessFeedbackReducer(HandlerCapsuleEffectivenessFeedback):
    """ONEX entry-point wrapper for HandlerCapsuleEffectivenessFeedback."""


__all__ = ["NodeCapsuleEffectivenessFeedbackReducer"]
