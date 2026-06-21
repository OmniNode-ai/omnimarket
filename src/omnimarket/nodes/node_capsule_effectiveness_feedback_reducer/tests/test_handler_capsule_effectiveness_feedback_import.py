# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_capsule_effectiveness_feedback."""

from omnimarket.nodes.node_capsule_effectiveness_feedback_reducer.handlers.handler_capsule_effectiveness_feedback import (
    HandlerCapsuleEffectivenessFeedback,
)


def test_handler_capsule_effectiveness_feedback_imports() -> None:
    handler = HandlerCapsuleEffectivenessFeedback()

    assert handler.subscribe_topic
    assert handler.claim_topic
    assert handler.hypothesis_topic
