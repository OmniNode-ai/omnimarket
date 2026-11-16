# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed policy for the ladder a rubric failure climbs: swap, shrink, escalate (OMN-20172)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)


class EnumShrinkRetryScope(StrEnum):
    """What failed the class rubric."""

    WHOLE = "whole"
    UNIT = "unit"
    RECOMBINED = "recombined"


class EnumShrinkRetryRung(StrEnum):
    """The next move of the ladder (plan 13.6 and 13.10).

    config_swap: retry once on the same model and backend with another configuration.
    shrink: split the request, run the units on the same model and backend.
    resplit_unit: a failed unit splits once more.
    escalate_unit: a failed unit goes alone to the next responder in the class chain.
    escalate_whole: the original request goes to the next responder in the class chain.
    """

    CONFIG_SWAP = "config_swap"
    SHRINK = "shrink"
    RESPLIT_UNIT = "resplit_unit"
    ESCALATE_UNIT = "escalate_unit"
    ESCALATE_WHOLE = "escalate_whole"


class EnumShrinkRetryReason(StrEnum):
    """Why the policy chose the rung, named for the attempt event."""

    SECOND_MET_CONFIGURATION = "second_met_configuration"
    DECOMPOSABLE = "decomposable"
    NOT_DECOMPOSABLE = "not_decomposable"
    SPLIT_DEPTH_CAP = "split_depth_cap"
    UNIT_ATTEMPT_CAP = "unit_attempt_cap"
    WHOLE_CHECK_FAILED = "whole_check_failed"
    SHRINK_NOT_DECLARED = "shrink_not_declared"


class ModelShrinkRetryCaps(BaseModel):
    """The class contract's caps (``escalation_policy``), never defaulted in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_config_swaps: int = Field(ge=0)
    max_split_depth: int = Field(ge=0)
    unit_attempts_per_unit: int = Field(ge=1)
    unit_prompt: str = Field(min_length=1)

    @model_validator(mode="after")
    def _unit_prompt_names_its_source(self) -> ModelShrinkRetryCaps:
        if "{source}" not in self.unit_prompt:
            raise ValueError("unit_prompt must contain the {source} placeholder")
        return self

    def prompt_for(self, source: str) -> str:
        """The prompt of one unit: the class's wording around only that unit's slice."""
        return self.unit_prompt.replace("{source}", source)


class ModelShrinkRetryDecisionRequest(BaseModel):
    """The ladder's state after one failed rung, and what the caller found out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    scope: EnumShrinkRetryScope
    caps: ModelShrinkRetryCaps | None
    config_swaps_used: int = Field(default=0, ge=0)
    split_depth: int = Field(default=0, ge=0)
    splittable: bool = False
    second_configuration: ModelAttemptConfiguration | None = None
    unit_count: int = Field(default=0, ge=0)
    unit_attempts_used: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _scope_needs_its_facts(self) -> ModelShrinkRetryDecisionRequest:
        if self.scope is EnumShrinkRetryScope.UNIT and self.unit_count < 2:
            raise ValueError("a unit failure needs the unit count of its split")
        return self


class ModelShrinkRetryDecision(BaseModel):
    """The rung to take next and the reason, for the attempt event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    rung: EnumShrinkRetryRung
    reason: EnumShrinkRetryReason
    configuration: ModelAttemptConfiguration | None = None


__all__ = [
    "EnumShrinkRetryReason",
    "EnumShrinkRetryRung",
    "EnumShrinkRetryScope",
    "ModelShrinkRetryCaps",
    "ModelShrinkRetryDecision",
    "ModelShrinkRetryDecisionRequest",
]
