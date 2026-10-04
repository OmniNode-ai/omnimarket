# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fixed text sections of the judge prompt."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_reject_condition import (
    ModelAcceptanceRejectCondition,
)


class ModelAcceptanceJudgePrompt(BaseModel):
    """The fixed text sections of the judge prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    preamble: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    reject_intro: str = Field(min_length=1)
    reject_conditions: tuple[ModelAcceptanceRejectCondition, ...] = Field(min_length=1)
    permitted_reply_rule: str = Field(min_length=1)
    omission_rule: str = Field(min_length=1)
    quality_scale: str = Field(min_length=1)
    failure_class_rule: str = Field(min_length=1)
    reason_rule: str = Field(min_length=1)
    closing_rule: str = Field(min_length=1)
    output_shape: str = Field(min_length=1)
