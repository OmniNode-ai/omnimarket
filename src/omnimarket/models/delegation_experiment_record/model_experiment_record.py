# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The results record of one experiment of the delegation experiment program."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation_experiment_record.enum_experiment_verdict import (
    EnumExperimentVerdict,
)
from omnimarket.models.delegation_experiment_record.model_experiment_arm_result import (
    ModelExperimentArmResult,
)


class ModelExperimentRecord(BaseModel):
    """Fields default to empty so the check reports a missing field as a typed refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    hypothesis: str = ""
    metric: str = ""
    baseline: float | None = None
    arms: tuple[ModelExperimentArmResult, ...] = ()
    verdict: EnumExperimentVerdict | None = None
    void_cause: str = ""
    follow_on_refs: tuple[str, ...] = ()
    declining_ruling_ref: str = ""
    changed_paths: tuple[str, ...] = ()
