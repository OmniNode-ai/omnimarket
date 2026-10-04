# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How many judged outputs of a cell failed in one class."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_failure_class import (
    EnumAcceptanceFailureClass,
)


class ModelAcceptanceFailureCount(BaseModel):
    """How many judged outputs of a cell failed in one class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_class: EnumAcceptanceFailureClass
    count: int = Field(ge=1)
