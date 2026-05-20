# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility re-export for the canonical delegate-skill response model."""

from __future__ import annotations

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
)

__all__ = [
    "ModelDelegateSkillResponse",
    "ModelDelegateSkillResponseMetrics",
]
