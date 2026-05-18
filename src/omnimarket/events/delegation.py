# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility imports for canonical delegation event payload models."""

from omnimarket.models.delegation.wire import (
    EnumQualityContractMode,
    ModelDelegationRequest,
    ModelDelegationResult,
    validate_acceptance_criteria,
)
from omnimarket.models.delegation.wire.model_delegation_request import (
    MAX_WORDS_PER_SENTENCE_RE,
    SUPPORTED_ACCEPTANCE_CRITERIA,
)

__all__ = [
    "MAX_WORDS_PER_SENTENCE_RE",
    "SUPPORTED_ACCEPTANCE_CRITERIA",
    "EnumQualityContractMode",
    "ModelDelegationRequest",
    "ModelDelegationResult",
    "validate_acceptance_criteria",
]
