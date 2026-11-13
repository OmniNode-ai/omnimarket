# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for the experiment result record compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.events.experiment_arm_result import ModelExperimentArmResult


class ModelExperimentResultRecordRequest(BaseModel):
    """One experiment-arm result command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: ModelExperimentArmResult


__all__ = ["ModelExperimentResultRecordRequest"]
