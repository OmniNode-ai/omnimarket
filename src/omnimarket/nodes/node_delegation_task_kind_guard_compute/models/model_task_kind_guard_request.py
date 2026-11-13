# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.model_task_kind_guard_tally import (
    ModelTaskKindGuardTally,
)


class ModelTaskKindGuardRequest(BaseModel):
    """One local answer to a classification question."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(..., min_length=1)
    task_kind: str = Field(..., min_length=1)
    answered_kind: str = Field(..., min_length=1)
    deterministic_kind: str | None = None
    tally: ModelTaskKindGuardTally = Field(default_factory=ModelTaskKindGuardTally)


__all__: list[str] = ["ModelTaskKindGuardRequest"]
