# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The identity of the judge run that the judged events carry."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_origin import (
    EnumAcceptanceOrigin,
)
from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
    check_origin_eval_run_id,
)


class ModelAcceptanceEventJudge(BaseModel):
    """What no verdict records: tenant, judge run and model, rubric hash, origin and time.

    The caller names these, so building events never samples the wall clock and a second build
    gives byte-identical events. origin has no default, as on the event itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    judge_run_id: str = Field(min_length=1)
    judge_model: str = Field(min_length=1)
    judge_model_version: str = Field(min_length=1)
    rubric_id: str = Field(min_length=1)
    rubric_hash: str = Field(min_length=1)
    calibration_run_id: str = Field(min_length=1)
    judged_at: AwareDatetime
    origin: EnumAcceptanceOrigin
    eval_run_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_eval_run_id(self) -> Self:
        check_origin_eval_run_id(self.origin, self.eval_run_id)
        return self
