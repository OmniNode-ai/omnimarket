# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review finding classifier: which findings hold a merge, and which become post-merge follow-ups.

A finding is BLOCKING when it names a failing check, or cites a code line (a file, a line and a defect). Every other
finding is ADVISORY, including one that asks for running proof (``installed=``, ``observed=``) or other evidence: an
advisory finding never holds a merge. Running proof stays the done gate and is not read here. No clock, no I/O and
no model call.
"""

from __future__ import annotations

from omnimarket.models.model_landing_review import (
    EnumReviewFindingClass,
    ModelLandingReviewFinding,
)


def classify_finding(finding: ModelLandingReviewFinding) -> EnumReviewFindingClass:
    """BLOCKING for a failing check, or for a file, line and defect together; ADVISORY otherwise."""
    if finding.failing_check is not None:
        return EnumReviewFindingClass.BLOCKING
    if (
        finding.file is not None
        and finding.line is not None
        and finding.defect is not None
    ):
        return EnumReviewFindingClass.BLOCKING
    return EnumReviewFindingClass.ADVISORY


__all__ = ["classify_finding"]
