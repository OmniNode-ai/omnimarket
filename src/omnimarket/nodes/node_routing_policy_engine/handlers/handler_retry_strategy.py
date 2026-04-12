# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Retry strategy engine — maps EnumFailureClass to EnumRetryType.

Two-Strike protocol: attempt_count >= _REPEATED_FAILURE_THRESHOLD always
escalates to TIER_ESCALATION regardless of failure class.

Related:
    - OMN-8044: Retry strategy engine
    - Architecture doc Section 9
"""

from __future__ import annotations

from enum import StrEnum

_REPEATED_FAILURE_THRESHOLD: int = 3


class EnumFailureClass(StrEnum):
    MISSING_CONTEXT = "missing_context"
    MALFORMED_OUTPUT = "malformed_output"
    INSUFFICIENT_REASONING = "insufficient_reasoning"
    MODEL_WEAKNESS = "model_weakness"


class EnumRetryType(StrEnum):
    IMPROVED_CONTEXT_RETRY = "improved_context_retry"
    SAME_MODEL_SAME_CONTEXT = "same_model_same_context"
    TIER_ESCALATION = "tier_escalation"
    PEER_MODEL = "peer_model"


_FAILURE_CLASS_MAP: dict[EnumFailureClass, EnumRetryType] = {
    EnumFailureClass.MISSING_CONTEXT: EnumRetryType.IMPROVED_CONTEXT_RETRY,
    EnumFailureClass.MALFORMED_OUTPUT: EnumRetryType.SAME_MODEL_SAME_CONTEXT,
    EnumFailureClass.INSUFFICIENT_REASONING: EnumRetryType.TIER_ESCALATION,
    EnumFailureClass.MODEL_WEAKNESS: EnumRetryType.PEER_MODEL,
}


def determine_retry_type(
    failure_class: EnumFailureClass,
    attempt_count: int,
) -> EnumRetryType:
    """Return the retry type for a given failure class and attempt count.

    Two-Strike override: any failure class at attempt_count >= 3 escalates to
    TIER_ESCALATION to prevent infinite retry loops.
    """
    if attempt_count >= _REPEATED_FAILURE_THRESHOLD:
        return EnumRetryType.TIER_ESCALATION
    return _FAILURE_CLASS_MAP[failure_class]


__all__: list[str] = [
    "EnumFailureClass",
    "EnumRetryType",
    "determine_retry_type",
]
