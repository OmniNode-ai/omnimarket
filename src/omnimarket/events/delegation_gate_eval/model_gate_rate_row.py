# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Labelled counts and range evaluations for one class, stratum and arm."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.delegation_gate_eval.model_wilson_interval import (
    ModelWilsonInterval,
)
from omnimarket.models.ranges import ModelRangeEvaluation


class ModelGateRateRow(BaseModel):
    """Labelled counts and range evaluations for one class, stratum and arm."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str
    stratum: str
    arm: Literal["replayed", "recorded"]
    accepted_n: int = Field(ge=0)
    false_pass_count: int = Field(ge=0)
    false_refusal_count: int = Field(ge=0)
    refused_n: int = Field(ge=0)
    undetermined_n: int = Field(ge=0)
    total_n: int = Field(ge=0)
    undetermined_share: float = Field(ge=0.0, le=1.0)
    false_pass_wilson: ModelWilsonInterval
    false_refusal_wilson: ModelWilsonInterval
    false_pass_evaluation: ModelRangeEvaluation
    false_refusal_evaluation: ModelRangeEvaluation
