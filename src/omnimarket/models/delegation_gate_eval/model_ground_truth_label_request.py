# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A batch of delegated runs to label from independent evidence (OMN-19787)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_gate_eval.model_ground_truth_run import (
    ModelGroundTruthRun,
)


class ModelGroundTruthLabelRequest(BaseModel):
    """Runs are labelled independently of one another and of the gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str = Field(min_length=1)
    runs: tuple[ModelGroundTruthRun, ...] = Field(min_length=1)
