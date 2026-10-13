# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, cell and result of the judged acceptance fold."""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.events import ModelDelegationAcceptanceJudgedEvent
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_origin import (
    EnumAcceptanceOrigin,
)
from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
    check_origin_eval_run_id,
)


class ModelJudgedAcceptanceFoldRequest(BaseModel):
    """The judged events to fold. The fold is a function of this set alone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: tuple[ModelDelegationAcceptanceJudgedEvent, ...] = ()


class ModelJudgedAcceptanceCell(BaseModel):
    """One cell: a model, task type and kind under one rubric version and one origin.

    An eval cell carries the ``eval_run_id`` of the one eval run it folds; a live cell has none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    task_type: str = Field(min_length=1)
    kind: Literal["task", "edit_loop_turn"]
    delegated_tier: str = Field(min_length=1)
    delegated_model_key: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    origin: EnumAcceptanceOrigin
    eval_run_id: str | None = Field(default=None, min_length=1)
    n: int = Field(ge=1)
    accepts: int = Field(ge=0)
    accept_rate: float = Field(ge=0.0, le=1.0)
    wilson_low: float = Field(ge=0.0, le=1.0)
    wilson_high: float = Field(ge=0.0, le=1.0)
    mean_quality: float = Field(ge=0.0, le=3.0)
    first_call_time: AwareDatetime
    last_call_time: AwareDatetime
    judge_run_count: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_eval_run_id(self) -> Self:
        check_origin_eval_run_id(self.origin, self.eval_run_id)
        return self


class ModelJudgedAcceptanceFoldResult(BaseModel):
    """The cells with at least the minimum verdicts, and every refusal counted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cells: tuple[ModelJudgedAcceptanceCell, ...] = ()
    folded_verdict_count: int = Field(ge=0, default=0)
    uncalibrated_refused_count: int = Field(ge=0, default=0)
    uncalibrated_judge_run_ids: tuple[str, ...] = ()
    excluded_kind_count: int = Field(ge=0, default=0)
    underpowered_cell_count: int = Field(ge=0, default=0)
