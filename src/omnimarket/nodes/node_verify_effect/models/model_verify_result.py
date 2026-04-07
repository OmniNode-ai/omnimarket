# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelVerifyResult — result from the verify effect node.

Related:
    - OMN-7581: migrate node_verify_effect to omnimarket
    - OMN-7575: Build loop migration epic
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_verify_effect.models.model_verify_check import (
    ModelVerifyCheck,
)


class ModelVerifyResult(BaseModel):
    """Result from the verify effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    all_critical_passed: bool = Field(
        ..., description="Whether all critical checks passed."
    )
    checks: tuple[ModelVerifyCheck, ...] = Field(
        ..., description="Individual check results."
    )
    warnings: tuple[str, ...] = Field(
        default_factory=tuple, description="Non-critical warnings."
    )


__all__: list[str] = ["ModelVerifyResult"]
