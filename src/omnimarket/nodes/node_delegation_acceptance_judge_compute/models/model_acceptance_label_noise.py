# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The calibration items on which both judges disagreed with the label."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_label_noise_case import (
    ModelAcceptanceLabelNoiseCase,
)


class ModelAcceptanceLabelNoise(BaseModel):
    """The calibration items on which both judges disagreed with the label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1)
    cases: tuple[ModelAcceptanceLabelNoiseCase, ...] = Field(min_length=1)
