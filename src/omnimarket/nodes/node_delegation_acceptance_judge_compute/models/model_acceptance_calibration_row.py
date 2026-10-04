# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One calibration measurement of a judge against labels or against another judge."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceCalibrationRow(BaseModel):
    """One calibration measurement of a judge against labels or against another judge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    judge: str = Field(min_length=1)
    item_set: str = Field(min_length=1)
    n: int = Field(ge=1)
    agreement: float = Field(ge=0.0, le=1.0)
    kappa: float = Field(ge=-1.0, le=1.0)
    false_accepts: int | None = Field(default=None, ge=0)
    false_rejects: int | None = Field(default=None, ge=0)
