# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility imports for shared delegation quality contract primitives."""

from omnimarket.events.delegation import (
    MAX_WORDS_PER_SENTENCE_RE,
    SUPPORTED_ACCEPTANCE_CRITERIA,
    EnumQualityContractMode,
    validate_acceptance_criteria,
)

__all__: list[str] = [
    "MAX_WORDS_PER_SENTENCE_RE",
    "SUPPORTED_ACCEPTANCE_CRITERIA",
    "EnumQualityContractMode",
    "validate_acceptance_criteria",
]
