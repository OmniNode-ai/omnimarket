# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility imports for canonical delegation event payload models."""

from omnibase_compat.contracts.delegation.wire import (
    MAX_WORDS_PER_SENTENCE_RE,
    SUPPORTED_ACCEPTANCE_CRITERIA,
    EnumQualityContractMode,
    ModelDelegationRequest,
    ModelDelegationResult,
    validate_acceptance_criteria,
)

__all__ = [
    "MAX_WORDS_PER_SENTENCE_RE",
    "SUPPORTED_ACCEPTANCE_CRITERIA",
    "EnumQualityContractMode",
    "ModelDelegationRequest",
    "ModelDelegationResult",
    "validate_acceptance_criteria",
]
