# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One refusal naming the experiment, and the arm and value that caused it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation_experiment_record.enum_experiment_refusal import (
    EnumExperimentRefusal,
)


class ModelExperimentIssue(BaseModel):
    """``arm_id`` and ``detail`` are empty when the refusal is about the whole record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refusal: EnumExperimentRefusal
    experiment_id: str
    arm_id: str = ""
    detail: str = ""
