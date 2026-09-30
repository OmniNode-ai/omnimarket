# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Computed criterion evidence with a validated snake-case reason."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_rubric_check_compute.models.enum_rubric_outcome import (
    EnumRubricOutcome,
)


class ModelRubricCriterionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    criterion_id: str = Field(min_length=1)
    outcome: EnumRubricOutcome
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    detail: str
    facts: tuple[str, ...]
