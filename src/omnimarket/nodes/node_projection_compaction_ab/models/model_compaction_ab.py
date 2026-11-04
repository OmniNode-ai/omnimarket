# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One row per boundary, written by three single-owner writers."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_projection_compaction_ab.models.model_floor_and_spec import (
    load_live_spec,
)


class EnumAbWriter(StrEnum):
    ASSIGNMENT = "assignment"
    COVARIATES = "covariates"
    SCORES = "scores"


class EnumArm(StrEnum):
    ON = "on"
    OFF = "off"


class EnumApplyReason(StrEnum):
    APPLIED = "applied"
    STALE_OR_DUPLICATE = "stale_or_duplicate"


class ModelAbRow(BaseModel):
    """The stored row. Each column belongs to exactly one writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    boundary_uuid: str = Field(min_length=1)
    # assignment writer
    arm: EnumArm | None = None
    block_index: int | None = None
    carryover: bool | None = None
    assignment_event_time: datetime | None = None
    assignment_event_id: str | None = None
    # covariates writer
    pre_compaction_tokens: int | None = None
    trigger: str | None = None
    rulings_in_window: int | None = None
    open_loops_at_boundary: int | None = None
    covariates_event_time: datetime | None = None
    covariates_event_id: str | None = None
    # scores writer: the live hypotheses and safeguards
    hl1_events: int | None = None
    hl2_acted_items: int | None = None
    hl2_open_loop_items: int | None = None
    incident_count: int | None = None
    hold_scope_violations: int | None = None
    s5_cost_tokens: int | None = None
    s3_defects: int | None = None
    s3_merged_prs: int | None = None
    scores_event_time: datetime | None = None
    scores_event_id: str | None = None


class ModelAbEvent(BaseModel):
    """A writer's observation of one boundary, ordered by (event_time, event_id)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    boundary_uuid: str = Field(min_length=1)
    writer: EnumAbWriter
    event_time: datetime
    event_id: str = Field(min_length=1)
    values: dict[str, str | int | bool]

    @model_validator(mode="after")
    def _only_owned_columns(self) -> Self:
        owned = set(load_live_spec().writers[self.writer.value].columns)
        foreign = set(self.values) - owned
        if foreign:
            raise ValueError(
                f"writer {self.writer.value} does not own columns {sorted(foreign)}"
            )
        return self


class ModelAbProjectionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelAbEvent
    previous_row: ModelAbRow | None = None


class ModelAbProjectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelAbRow
    reason: EnumApplyReason

    @property
    def applied(self) -> bool:
        return self.reason is EnumApplyReason.APPLIED


__all__ = [
    "EnumAbWriter",
    "EnumApplyReason",
    "EnumArm",
    "ModelAbEvent",
    "ModelAbProjectionRequest",
    "ModelAbProjectionResult",
    "ModelAbRow",
]
