# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Quality gate intent model emitted by the delegation orchestrator."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)


class ModelQualityGateIntent(BaseModel):
    """Intent emitted when the orchestrator requests quality gate evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: str = Field(default="quality_gate")
    payload: ModelQualityGateInput


__all__: list[str] = ["ModelQualityGateIntent"]
