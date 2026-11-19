# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The state_io row of one code eval run (OMN-23627; formal/code_eval_trial).

One row per run, keyed by the run's correlation id. The row is advanced by the
reducer under compare-and-set; it is mutable so the reducer can edit a deep copy
and compare it with the stored row to learn that a message changed nothing.
A slot is pending, a try in flight, a score queued, a score in flight or done.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_eval_unscorable_reason import EnumEvalUnscorableReason
from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalRunRequest,
)
from omnimarket.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)


class EnumCodeEvalSlot(StrEnum):
    PENDING = "pending"
    TRY_IN_FLIGHT = "try_in_flight"
    SCORE_QUEUED = "score_queued"
    SCORE_IN_FLIGHT = "score_in_flight"
    DONE = "done"


class ModelCodeEvalSettings(BaseModel):
    """The overlay and contract values frozen at the run leg (every one from a declared source)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_budgets: tuple[int, ...] = Field(min_length=1)
    try_timeout_seconds: int = Field(ge=1)
    score_timeout_seconds: int = Field(ge=1)
    receipt_slack_seconds: int = Field(ge=0)
    nudge_interval_seconds: int = Field(ge=1)
    completion_bound_seconds: int = Field(ge=1)
    infra_retries: int = Field(default=0, ge=0)
    void_infra_first_try_percent: int = Field(default=10, ge=0, le=100)


class ModelCodeEvalVerdict(BaseModel):
    """E2's verdict (and its source when unscorable) for one try or control."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumCodeScoreVerdict
    source: EnumCodeScoreSource | None = None
    #: The typed cause of an unscorable try the driver itself named (revision_unobserved, registration_mismatch).
    reason: EnumEvalUnscorableReason | None = None

    @property
    def label(self) -> str:
        if self.reason is not None:
            return f"{self.verdict.value}({self.reason.value})"
        return (
            f"{self.verdict.value}({self.source.value})"
            if self.source
            else self.verdict.value
        )


class ModelCodeEvalTaskRow(BaseModel):
    """One admitted task: its band and its control's slot and verdict."""

    model_config = ConfigDict(extra="forbid")

    task_key: str
    band: str
    control_slot: EnumCodeEvalSlot = EnumCodeEvalSlot.SCORE_QUEUED
    control_queued_seq: int = 0
    control_sent_at: datetime | None = None
    control: ModelCodeEvalVerdict | None = None


class ModelCodeEvalSequenceRow(BaseModel):
    """One task's try sequence for one model arm."""

    model_config = ConfigDict(extra="forbid")

    task_key: str
    model: str
    backend_id: str
    slot: EnumCodeEvalSlot = EnumCodeEvalSlot.PENDING
    verdicts: list[ModelCodeEvalVerdict] = Field(default_factory=list)
    unit_id: str = ""
    host: str = ""
    sent_at: datetime | None = None
    queued_seq: int = 0
    draft_diff: str = ""
    timed_out_with_diff: bool = False
    loop_status: str = ""
    duration_s: float = 0.0
    #: The revision the endpoint reported for the try now in flight or scoring (empty: none reported).
    model_revision: str = ""
    #: Infrastructure retries spent on the current ordinal.
    retries_used: int = 0
    #: The first attempt of ordinal 1 was infrastructure-unscorable (before any retry).
    first_try_infra: bool = False


class ModelCodeEvalRunRow(BaseModel):
    """The whole row."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    request: ModelCodeEvalRunRequest
    settings: ModelCodeEvalSettings
    digest: str
    excluded_counts: dict[str, int] = Field(default_factory=dict)
    tasks: list[ModelCodeEvalTaskRow] = Field(default_factory=list)
    sequences: list[ModelCodeEvalSequenceRow] = Field(default_factory=list)
    next_seq: int = 1
    in_flight: bool = True
    state: str = "RUNNING"
    closed: bool = False
    #: The revision each model arm's first observed try registered (model -> revision).
    registered_revisions: dict[str, str] = Field(default_factory=dict)
    #: A try reported a revision other than its arm's registered one.
    revision_changed: bool = False
    updated_at: datetime


__all__ = [
    "EnumCodeEvalSlot",
    "ModelCodeEvalRunRow",
    "ModelCodeEvalSequenceRow",
    "ModelCodeEvalSettings",
    "ModelCodeEvalTaskRow",
    "ModelCodeEvalVerdict",
]
