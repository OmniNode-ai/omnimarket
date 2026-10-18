# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recorded outcome of one task-kind rule condition."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_context_task_kind_operator import (
    EnumContextTaskKindOperator,
)


class ModelContextTaskKindConditionOutcome(BaseModel):
    """Whether a condition held; ``absent`` when the caller did not supply the field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    scope_field: str = Field(min_length=1)
    operator: EnumContextTaskKindOperator
    value: str = Field(min_length=1)
    negate: bool
    absent: bool
    matched: bool


__all__ = ["ModelContextTaskKindConditionOutcome"]
