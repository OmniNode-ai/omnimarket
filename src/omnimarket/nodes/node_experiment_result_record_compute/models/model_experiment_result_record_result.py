# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for the experiment result record compute node."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.experiment_result.model_experiment_arm_result import (
    ModelExperimentArmResult,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.enum_experiment_result_record_status import (
    EnumExperimentResultRecordStatus,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.model_experiment_result_refusal import (
    ModelExperimentResultRefusal,
)


class ModelExperimentResultRecordResult(BaseModel):
    """The typed completed or failed terminal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumExperimentResultRecordStatus
    result_id: UUID
    result: ModelExperimentArmResult
    accuracy: float
    refusals: tuple[ModelExperimentResultRefusal, ...] = ()

    @model_validator(mode="after")
    def _status_matches_refusals(self) -> ModelExperimentResultRecordResult:
        if self.status is EnumExperimentResultRecordStatus.COMPLETED and self.refusals:
            raise ValueError("COMPLETED status requires no refusals")
        if self.status is EnumExperimentResultRecordStatus.FAILED and not self.refusals:
            raise ValueError("FAILED status requires at least one refusal")
        return self


__all__ = ["ModelExperimentResultRecordResult"]
