# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A declared task-kind rule: selector data, never a heuristic (plan section 4.4)."""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_context_task_kind_condition_logic import (
    EnumContextTaskKindConditionLogic,
)
from omnimarket.enums.enum_context_task_kind_priority import (
    EnumContextTaskKindPriority,
)
from omnimarket.models.context_pack.model_context_task_kind_condition import (
    ModelContextTaskKindCondition,
)

#: The task kind of a request no rule matched.
TASK_KIND_UNKNOWN = "unknown"


class ModelContextTaskKindRule(BaseModel):
    """Typed conditions over the request's caller scope that admit ``factors``.

    The conditions are combined by ``condition_logic``; a rule admits its factors
    only when it matches. Nothing is derived from prompt text: keyword and
    conversation-domain conditions do not exist in this vocabulary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1)
    task_kind: str = Field(min_length=1)
    priority: EnumContextTaskKindPriority = EnumContextTaskKindPriority.MEDIUM
    condition_logic: EnumContextTaskKindConditionLogic = (
        EnumContextTaskKindConditionLogic.ALL
    )
    conditions: tuple[ModelContextTaskKindCondition, ...] = Field(min_length=1)
    factors: tuple[EnumContextFactor, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _task_kind_is_not_unknown(self) -> ModelContextTaskKindRule:
        if self.task_kind == TASK_KIND_UNKNOWN:
            raise ValueError(
                f"task_kind_rules[{self.rule_id}] may not declare task_kind "
                f"{TASK_KIND_UNKNOWN!r}; it is the unmatched result"
            )
        return self


__all__ = ["TASK_KIND_UNKNOWN", "ModelContextTaskKindRule"]
