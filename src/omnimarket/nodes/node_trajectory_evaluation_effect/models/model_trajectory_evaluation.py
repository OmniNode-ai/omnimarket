# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Commands, outcomes and contract blocks for node_trajectory_evaluation_effect (OMN-20087).

The two commands are loosely typed on purpose: a malformed command must reach
the handler and be refused ``input_invalid`` where a consumer can see it,
rather than fail coercion at the bus and go to the dead-letter topic.

The four outcome classes carry no handling wall-clock time, so a replayed
command produces identical bytes. Time lives on the envelope.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumTrajectoryEvaluationReason(StrEnum):
    """Why a command was refused or failed."""

    # Refused: a policy decision, never retried.
    BACKEND_DISABLED = "backend_disabled"
    BACKEND_MISCONFIGURED = "backend_misconfigured"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    INPUT_INVALID = "input_invalid"
    CONTENT_MODE_NOT_PERMITTED = "content_mode_not_permitted"
    REPOSITORY_NOT_PUBLIC = "repository_not_public"
    CREDIT_RESERVATION_EXHAUSTED = "credit_reservation_exhausted"
    CREDIT_STOP = "credit_stop"
    TARGET_REJECTED = "target_rejected"
    # Failed: retryable.
    GUARD_RATE_LIMITED = "guard_rate_limited"
    GUARD_UNRESOLVED = "guard_unresolved"
    EVALUATOR_RATE_LIMITED = "evaluator_rate_limited"
    EVALUATOR_TRANSPORT = "evaluator_transport"


class _ModelFrozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class ModelTrajectoryEvaluationSubmit(_ModelFrozen):
    """Submit one delegation trajectory for evaluation."""

    correlation_id: UUID
    work_unit_repository: str | None = None
    content_mode: str | None = None
    requested_at: str | None = None
    model_id: str | None = None
    provider: str | None = None
    task_type: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    quality_score: float | None = None
    gate_result: str | None = None
    prompt_text: str | None = None
    response_text: str | None = None


class ModelTrajectoryEvaluationPoll(_ModelFrozen):
    """Read the verdict of an earlier accepted submission once."""

    correlation_id: UUID
    content_mode: str | None = None
    backend_receipt_id: str | None = None


class ModelTrajectoryEvaluationAccepted(_ModelFrozen):
    """The evaluator accepted the submission; carries a terminal verdict when the bounded poll reached one."""

    correlation_id: UUID
    content_mode: str
    idempotency_key: str
    backend_receipt_id: str
    duplicate: bool
    verdict_state: Literal["completed", "pending"]
    verdict: str | None = None


class ModelTrajectoryEvaluationRefused(_ModelFrozen):
    """A policy decision. Never retried."""

    correlation_id: UUID
    reason: EnumTrajectoryEvaluationReason
    detail: str = ""


class ModelTrajectoryEvaluationFailed(_ModelFrozen):
    """Transport, timeout or rate limit. Retryable."""

    correlation_id: UUID
    reason: EnumTrajectoryEvaluationReason
    retryable: bool = True
    detail: str = ""


class ModelTrajectoryEvaluationStatus(_ModelFrozen):
    """The result of one poll command."""

    correlation_id: UUID
    backend_receipt_id: str
    verdict_state: Literal["completed", "pending"]
    verdict: str | None = None


ModelTrajectoryEvaluationCommand = (
    ModelTrajectoryEvaluationSubmit | ModelTrajectoryEvaluationPoll
)
ModelTrajectoryEvaluationOutcome = (
    ModelTrajectoryEvaluationAccepted
    | ModelTrajectoryEvaluationRefused
    | ModelTrajectoryEvaluationFailed
    | ModelTrajectoryEvaluationStatus
)


class ModelWorkUnitScoping(_ModelFrozen):
    """The contract's ``work_unit_scoping`` block."""

    require_public_repository: bool
    visibility_timeout_ms: int = Field(gt=0)


class ModelEvaluationPacing(_ModelFrozen):
    """The contract's ``evaluation_pacing`` block."""

    submits_per_minute: int = Field(gt=0)
    status_reads_per_minute: int = Field(gt=0)
    max_in_flight: int = Field(gt=0)
    daily_reserved_credits: int = Field(ge=0)
    credits_per_full_text_command: int = Field(ge=0)
    credits_per_metadata_command: int = Field(ge=0)


class ModelStatusPoll(_ModelFrozen):
    """The contract's ``status_poll`` block."""

    interval_seconds: int = Field(gt=0)
    max_wait_seconds: int = Field(ge=0)

    @property
    def max_reads(self) -> int:
        return 1 + self.max_wait_seconds // self.interval_seconds
