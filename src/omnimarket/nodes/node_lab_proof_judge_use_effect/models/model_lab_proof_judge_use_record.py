# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed record of one lab proof judge use."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_lab_proof_judge_use_effect.models.enum_use_outcome import (
    EnumUseOutcome,
)


class ModelLabProofJudgeUseRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    pr_number: int = Field(ge=1)
    head: str = Field(pattern=r"^[0-9a-f]{40}$")
    class_id: str = Field(min_length=1)
    outcome: EnumUseOutcome
    nodes: tuple[str, ...] = Field(min_length=1)
    receipt_ids: tuple[str, ...] = ()
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be tz-aware")
        return value


__all__ = ["ModelLabProofJudgeUseRecord"]
