# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A declared task-kind rule: selector data, never a heuristic (plan section 4.4)."""

from __future__ import annotations

from typing import Literal

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelContextTaskKindRule(BaseModel):
    """One condition over a field of the request's caller scope.

    A rule matches when the caller scope's ``scope_field`` equals ``equals`` or,
    for the path field, matches ``glob``. A descriptor the caller does not supply
    never matches; nothing is derived from prompt text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1)
    task_kind: str = Field(min_length=1)
    scope_field: Literal[
        "task_class",
        "node_archetype",
        "request_paths",
        "repository",
        "lane",
        "lane_kind",
        "cited_ruling_stamps",
        "cited_plan_paths",
    ]
    equals: str | None = Field(default=None, min_length=1)
    glob: str | None = Field(default=None, min_length=1)
    factors: tuple[EnumContextFactor, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _exactly_one_condition(self) -> ModelContextTaskKindRule:
        if (self.equals is None) == (self.glob is None):
            raise ValueError(
                f"task_kind_rules[{self.rule_id}] needs exactly one of equals or glob"
            )
        return self


__all__ = ["ModelContextTaskKindRule"]
