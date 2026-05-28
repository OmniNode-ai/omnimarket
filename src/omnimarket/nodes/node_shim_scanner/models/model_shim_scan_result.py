# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result model for node_shim_scanner."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_shim_scanner.models.model_shim_finding import (
    ModelShimFinding,
)

__all__ = ["ModelShimScanResult"]


class ModelShimScanResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: list[ModelShimFinding] = Field(default_factory=list)
    expired_count: int = Field(default=0, ge=0)
    expiring_count: int = Field(default=0, ge=0)
    total_count: int = Field(default=0, ge=0)
