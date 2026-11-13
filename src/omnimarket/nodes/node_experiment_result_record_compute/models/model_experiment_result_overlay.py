# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment overlay for the experiment result record compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_experiment_result_record_compute.models.model_experiment_result_policy import (
    ModelExperimentResultPolicy,
)


class ModelExperimentResultOverlay(BaseModel):
    """The overlay file also carries an event_bus block the bus reader owns."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    policy: ModelExperimentResultPolicy


__all__ = ["ModelExperimentResultOverlay"]
