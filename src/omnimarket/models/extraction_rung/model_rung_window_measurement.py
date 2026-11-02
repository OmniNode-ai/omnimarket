# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A measured window of one rung (OMN-21140, plan task TE.3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRungWindowMeasurement(BaseModel):
    """window_tokens is the largest prompt a rung completed whole under load_condition.

    chars_per_token is measured on the same calls. slots_busy is how many of the
    rung's serving slots were busy during the measurement.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    measurement_id: str = Field(min_length=1)
    rung_id: str = Field(min_length=1)
    window_tokens: int = Field(ge=1)
    chars_per_token: float = Field(gt=0)
    load_condition: str = Field(min_length=1)
    slots_busy: int = Field(ge=0)
    measured_at: str = Field(min_length=1)
