# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Policy for the experiment result record compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelExperimentResultPolicy(BaseModel):
    """The cross-field check policy; a missing or empty refused_engines must fail validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_tolerance: float = Field(gt=0.0, le=0.1)
    confidence_z: float = Field(gt=0.0)
    refused_engines: tuple[str, ...] = Field(min_length=1)


__all__ = ["ModelExperimentResultPolicy"]
