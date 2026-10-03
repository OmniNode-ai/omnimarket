# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the rubric thresholds came from: the calibration numbers and label noise."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_calibration_row import (
    ModelAcceptanceCalibrationRow,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_calibration_set import (
    ModelAcceptanceCalibrationSet,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_corpus_facts import (
    ModelAcceptanceCorpusFacts,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_label_noise import (
    ModelAcceptanceLabelNoise,
)


class ModelAcceptanceProvenance(BaseModel):
    """Where the rubric thresholds came from: the calibration numbers and label noise."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    calibration_set: ModelAcceptanceCalibrationSet
    judges: tuple[ModelAcceptanceCalibrationRow, ...] = Field(min_length=1)
    corpus: ModelAcceptanceCorpusFacts
    codex_is_stricter: str = Field(min_length=1)
    label_noise: ModelAcceptanceLabelNoise
