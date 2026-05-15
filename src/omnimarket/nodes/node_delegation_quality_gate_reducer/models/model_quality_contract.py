# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility imports for canonical delegation quality contract primitives."""

from omnibase_compat.contracts.delegation.wire import (
    EnumQualityContractMode,
    validate_acceptance_criteria,
)
from omnibase_compat.contracts.delegation.wire.model_delegation_request import (
    MAX_WORDS_PER_SENTENCE_RE,
    SUPPORTED_ACCEPTANCE_CRITERIA,
)

__all__: list[str] = [
    "MAX_WORDS_PER_SENTENCE_RE",
    "SUPPORTED_ACCEPTANCE_CRITERIA",
    "EnumQualityContractMode",
    "validate_acceptance_criteria",
]
