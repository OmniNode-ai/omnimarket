# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A committed judged run, and the identity of its judge, to be replayed as events."""

from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_row import (
    ModelAcceptanceReplayRow,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_summary import (
    ModelAcceptanceReplaySummary,
)


class ModelAcceptanceReplayRequest(BaseModel):
    """The lines of a committed judgments.jsonl, its summary.json, and what neither records.

    A committed run holds neither the delegated tier, the tenant, the judge's model version,
    the rubric hash nor the time of judgment; the caller names them, so replay never samples
    the wall clock and a second replay builds byte-identical events.
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
