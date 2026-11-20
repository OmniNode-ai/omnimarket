# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic projection input: one recorded experiment-arm result."""

from pydantic import ConfigDict

from omnimarket.models.experiment_result.model_experiment_arm_result import (
    ModelExperimentArmResultRecorded,
)


class ModelExperimentResultProjectionRequest(ModelExperimentArmResultRecorded):
    """The record node's recorded terminal, as this projection receives it.

    Every field the fold reads is carried by the producer inside ``result``;
    ``measured_at`` comes from there too and is required -- nothing is
    invented from the envelope. ``extra="ignore"`` because the runtime adds
    envelope keys and the terminal also carries a status and the refusals.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")
