# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Compatibility import for the OMN-13366 quality gate result DTO.

``ModelQualityGateInput`` stays canonical in omnibase_core. The result carries
omnimarket P1 deterministic acceptance evidence fields until those fields are
promoted into the shared core wire DTO.
"""

from omnimarket.models.delegation.wire.model_quality_gate import (
    EnumQualityGateCategory,
    ModelQualityGateResult,
)

__all__: list[str] = ["EnumQualityGateCategory", "ModelQualityGateResult"]
