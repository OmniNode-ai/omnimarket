# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical delegation event payload models (graduated to omnibase_core, OMN-12659)."""

from omnibase_core.models.delegation.wire import (
    MAX_WORDS_PER_SENTENCE_RE,
    SUPPORTED_ACCEPTANCE_CRITERIA,
    EnumQualityContractMode,
    ModelDelegationRequest,
    ModelDelegationResult,
    ModelInferenceResponseData,
    validate_acceptance_criteria,
)

__all__ = [
    "MAX_WORDS_PER_SENTENCE_RE",
    "SUPPORTED_ACCEPTANCE_CRITERIA",
    "EnumQualityContractMode",
    "ModelDelegationRequest",
    "ModelDelegationResult",
    "ModelInferenceResponseData",
    "validate_acceptance_criteria",
]
