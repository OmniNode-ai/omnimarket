# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility re-export for the canonical delegate-skill request model."""

from __future__ import annotations

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    DELEGATION_DEFAULT_MAX_TOKENS,
    DELEGATION_MAX_TOKENS_HARD_LIMIT,
    EnumQualityContractMode,
    ModelDelegateSkillRequest,
)

__all__: list[str] = [
    "DELEGATION_DEFAULT_MAX_TOKENS",
    "DELEGATION_MAX_TOKENS_HARD_LIMIT",
    "EnumQualityContractMode",
    "ModelDelegateSkillRequest",
]
