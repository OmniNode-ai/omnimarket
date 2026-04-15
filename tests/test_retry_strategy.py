# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for retry strategy engine (OMN-8044).

Covers the five DoD test cases:
- MISSING_CONTEXT -> IMPROVED_CONTEXT_RETRY
- MALFORMED_OUTPUT -> SAME_MODEL_SAME_CONTEXT
- INSUFFICIENT_REASONING -> TIER_ESCALATION
- MODEL_WEAKNESS -> PEER_MODEL
- attempt_count >= 3 always overrides to TIER_ESCALATION (Two-Strike protocol)
"""

from __future__ import annotations

from omnimarket.nodes.node_routing_policy_engine.handlers.handler_retry_strategy import (
    EnumFailureClass,
    EnumRetryType,
    determine_retry_type,
)


def test_missing_context_maps_to_improved_context_retry() -> None:
    result = determine_retry_type(EnumFailureClass.MISSING_CONTEXT, attempt_count=1)
    assert result == EnumRetryType.IMPROVED_CONTEXT_RETRY


def test_malformed_output_maps_to_same_model_same_context() -> None:
    result = determine_retry_type(EnumFailureClass.MALFORMED_OUTPUT, attempt_count=1)
    assert result == EnumRetryType.SAME_MODEL_SAME_CONTEXT


def test_insufficient_reasoning_maps_to_tier_escalation() -> None:
    result = determine_retry_type(
        EnumFailureClass.INSUFFICIENT_REASONING, attempt_count=1
    )
    assert result == EnumRetryType.TIER_ESCALATION


def test_model_weakness_maps_to_peer_model() -> None:
    result = determine_retry_type(EnumFailureClass.MODEL_WEAKNESS, attempt_count=1)
    assert result == EnumRetryType.PEER_MODEL


def test_repeated_failures_override_to_tier_escalation() -> None:
    """attempt_count >= 3 always returns TIER_ESCALATION regardless of failure class."""
    for failure_class in EnumFailureClass:
        result = determine_retry_type(failure_class, attempt_count=3)
        assert result == EnumRetryType.TIER_ESCALATION, (
            f"Expected TIER_ESCALATION for {failure_class} at attempt_count=3, got {result}"
        )
    # Also verify attempt_count=4, 5
    result4 = determine_retry_type(EnumFailureClass.MISSING_CONTEXT, attempt_count=4)
    assert result4 == EnumRetryType.TIER_ESCALATION
    result5 = determine_retry_type(EnumFailureClass.MALFORMED_OUTPUT, attempt_count=5)
    assert result5 == EnumRetryType.TIER_ESCALATION
