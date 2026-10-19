# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recorded outcome of one task-kind rule: matched or failed, with every condition."""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_context_task_kind_priority import (
    EnumContextTaskKindPriority,
)
from omnimarket.models.context_task_kind_rules.model_context_task_kind_condition_outcome import (
    ModelContextTaskKindConditionOutcome,
)


class ModelContextTaskKindRuleOutcome(BaseModel):
    """A rule's identity, whether it matched, and each condition's outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1)
    task_kind: str = Field(min_length=1)
    priority: EnumContextTaskKindPriority
    matched: bool
    factors: tuple[EnumContextFactor, ...]
    conditions: tuple[ModelContextTaskKindConditionOutcome, ...]


__all__ = ["ModelContextTaskKindRuleOutcome"]
