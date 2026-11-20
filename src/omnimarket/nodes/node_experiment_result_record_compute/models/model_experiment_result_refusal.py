# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One refusal of the experiment result record compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_experiment_result_record_compute.models.enum_experiment_result_refusal import (
    EnumExperimentResultRefusal,
)


class ModelExperimentResultRefusal(BaseModel):
    """One violated cross-field rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refusal: EnumExperimentResultRefusal
    detail: str


__all__ = ["ModelExperimentResultRefusal"]
