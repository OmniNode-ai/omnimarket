# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Content-free offline judge verdict, with calibration and replay identity."""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_failure_class import (
    EnumAcceptanceFailureClass,
)

# UUID5(NAMESPACE_URL, the judged-acceptance topic); stable across replay.
JUDGED_ACCEPTANCE_ID_NAMESPACE = UUID("863551bd-bafa-5c15-97b8-79deed7316c0")


def judged_acceptance_event_id_for(judge_run_id: str, correlation_id: UUID) -> UUID:
    """One delivery key per judge run and delegated output, independent of verdict."""
    return uuid5(
        uuid5(JUDGED_ACCEPTANCE_ID_NAMESPACE, judge_run_id), str(correlation_id)
    )


class ModelDelegationAcceptanceJudgedEvent(BaseModel):
    """One offline verdict with the judged call's placement and calibration.

    Calibration thresholds belong to the projection fold. This schema preserves
    measured calibration, including a run the fold will refuse. Event time comes
    from the call and judge; construction never samples the wall clock.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    correlation_id: UUID
    tenant_id: UUID
    delegated_model_key: str = Field(min_length=1)
    delegated_tier: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    kind: Literal["task", "edit_loop_turn"]
    call_time: AwareDatetime

    judge_run_id: str = Field(min_length=1)
    judge_model: str = Field(min_length=1)
    judge_model_version: str = Field(min_length=1)
    rubric_id: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    rubric_hash: str = Field(min_length=1)

    calibration_run_id: str = Field(min_length=1)
    calibration_n: int = Field(strict=True, gt=0)
    calibration_agreement: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    calibration_kappa: float = Field(ge=-1.0, le=1.0, allow_inf_nan=False)

    accept: bool = Field(strict=True)
    quality: int = Field(strict=True, ge=0, le=3)
    failure_class: EnumAcceptanceFailureClass
    judged_at: AwareDatetime
    event_id: UUID = Field(
        default_factory=lambda data: judged_acceptance_event_id_for(
            data["judge_run_id"], data["correlation_id"]
        )
    )

    @model_validator(mode="after")
    def _validate_event_id(self) -> Self:
        if self.event_id != judged_acceptance_event_id_for(
            self.judge_run_id, self.correlation_id
        ):
            raise ValueError("event_id must match judge_run_id and correlation_id")
        return self


__all__ = [
    "JUDGED_ACCEPTANCE_ID_NAMESPACE",
    "ModelDelegationAcceptanceJudgedEvent",
    "judged_acceptance_event_id_for",
]
