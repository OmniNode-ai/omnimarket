# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result: the task kind, the factors the profile admits for it, and every rule's outcome."""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_context_task_kind_rules_compute.models.enum_context_task_kind_rules_status import (
    EnumContextTaskKindRulesStatus,
)
from omnimarket.nodes.node_context_task_kind_rules_compute.models.model_context_task_kind_rule_outcome import (
    ModelContextTaskKindRuleOutcome,
)


class ModelContextTaskKindRulesResult(BaseModel):
    """``task_kind`` is ``unknown`` when no rule matched; then only unconditional factors load.

    ``matched_rules`` are ordered by priority, then rule id; the first names the task
    kind. ``admitted_factors`` follow the profile's ``factor_precedence``;
    ``withheld_factors`` are the conditional factors no matched rule admitted. On
    ``error`` no factor is admitted and ``error`` names the failure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumContextTaskKindRulesStatus
    profile_name: str = Field(min_length=1)
    task_kind: str | None = None
    matched_rules: tuple[ModelContextTaskKindRuleOutcome, ...] = ()
    unmatched_rules: tuple[ModelContextTaskKindRuleOutcome, ...] = ()
    admitted_factors: tuple[EnumContextFactor, ...] = ()
    withheld_factors: tuple[EnumContextFactor, ...] = ()
    request_id: str = ""
    error: str | None = None


__all__ = ["ModelContextTaskKindRulesResult"]
