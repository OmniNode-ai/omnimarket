# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, cell and result of the judged acceptance fold."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.events import ModelDelegationAcceptanceJudgedEvent


class ModelJudgedAcceptanceFoldRequest(BaseModel):
    """The judged events to fold. The fold is a function of this set alone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: tuple[ModelDelegationAcceptanceJudgedEvent, ...] = ()


class ModelJudgedAcceptanceCell(BaseModel):
    """One cell: a model, task type and kind under one rubric version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    task_type: str = Field(min_length=1)
    kind: Literal["task", "edit_loop_turn"]
    delegated_tier: str = Field(min_length=1)
    delegated_model_key: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    n: int = Field(ge=1)
    accepts: int = Field(ge=0)
    accept_rate: float = Field(ge=0.0, le=1.0)
    wilson_low: float = Field(ge=0.0, le=1.0)
    wilson_high: float = Field(ge=0.0, le=1.0)
    mean_quality: float = Field(ge=0.0, le=3.0)
    first_call_time: AwareDatetime
    last_call_time: AwareDatetime
    judge_run_count: int = Field(ge=1)


class ModelJudgedAcceptanceFoldResult(BaseModel):
    """The cells with at least the minimum verdicts, and every refusal counted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cells: tuple[ModelJudgedAcceptanceCell, ...] = ()
    folded_verdict_count: int = Field(ge=0, default=0)
    uncalibrated_refused_count: int = Field(ge=0, default=0)
    uncalibrated_judge_run_ids: tuple[str, ...] = ()
    excluded_kind_count: int = Field(ge=0, default=0)
    underpowered_cell_count: int = Field(ge=0, default=0)
