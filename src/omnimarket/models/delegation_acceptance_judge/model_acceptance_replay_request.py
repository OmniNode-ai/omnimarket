# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A committed judged run, and the identity of its judge, to be replayed as events."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_origin import (
    EnumAcceptanceOrigin,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_row import (
    ModelAcceptanceReplayRow,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_summary import (
    ModelAcceptanceReplaySummary,
)
from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
    check_origin_eval_run_id,
)


class ModelAcceptanceReplayRequest(BaseModel):
    """The lines of a committed judgments.jsonl, its summary.json, and what neither records.

    A committed run holds neither the delegated tier, the tenant, the judge's model version,
    the rubric hash nor the time of judgment; the caller names them, so replay never samples
    the wall clock and a second replay builds byte-identical events.

    ``origin`` is where the run's verdicts came from. A committed run that predates the origin
    amendment judged live delegation traffic, so the default is ``live``: this request is the one
    place that mapping is made, and the event schema itself never defaults. Replaying an eval run
    names ``origin: eval`` and its ``eval_run_id``; the same rule as the event applies.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelAcceptanceReplayRow, ...]
    summary: ModelAcceptanceReplaySummary
    tenant_id: UUID
    tier_by_model: dict[str, str]
    judge_run_id: str = Field(min_length=1)
    judge_model: str = Field(min_length=1)
    judge_model_version: str = Field(min_length=1)
    rubric_id: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    rubric_hash: str = Field(min_length=1)
    calibration_run_id: str = Field(min_length=1)
    judged_at: AwareDatetime
    origin: EnumAcceptanceOrigin = EnumAcceptanceOrigin.LIVE
    eval_run_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_eval_run_id(self) -> Self:
        check_origin_eval_run_id(self.origin, self.eval_run_id)
        return self
