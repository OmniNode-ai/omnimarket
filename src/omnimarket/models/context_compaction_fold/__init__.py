# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared compaction receipts fold models (OMN-17427, TA.13)."""

from omnimarket.models.context_compaction_fold.model_context_compaction_fold import (
    EnumCompactionActivationState,
    EnumCompactionCandidateState,
    ModelCompactionActivationFold,
    ModelCompactionCandidateFold,
    ModelCompactionFallbackFold,
    ModelCompactionFoldRequest,
    ModelCompactionFoldResult,
)

__all__ = [
    "EnumCompactionActivationState",
    "EnumCompactionCandidateState",
    "ModelCompactionActivationFold",
    "ModelCompactionCandidateFold",
    "ModelCompactionFallbackFold",
    "ModelCompactionFoldRequest",
    "ModelCompactionFoldResult",
]
