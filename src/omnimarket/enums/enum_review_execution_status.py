# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How one review arm ended (OMN-21188)."""

from enum import StrEnum, unique


@unique
class EnumReviewExecutionStatus(StrEnum):
    """Whether an arm's review ran to the end, ran in part, or did not run."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


@unique
class EnumReviewThreadKind(StrEnum):
    """What a finding was posted as: a thread anchored to code, or a loose comment."""

    REVIEW_THREAD = "review_thread"
    GENERAL_COMMENT = "general_comment"


__all__ = ["EnumReviewExecutionStatus", "EnumReviewThreadKind"]
