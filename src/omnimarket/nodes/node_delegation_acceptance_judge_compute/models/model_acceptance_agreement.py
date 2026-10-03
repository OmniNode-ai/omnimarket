# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Agreement between the primary and the second judge on the double-judged items."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceAgreement(BaseModel):
    """Agreement between the primary and the second judge on the double-judged items."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    double_judged: int = Field(ge=0)
    required_double_judged: int = Field(ge=0)
    agreed: int = Field(ge=0)
    agreement: float = Field(ge=0.0, le=1.0)
    kappa: float = Field(ge=-1.0, le=1.0)
    kappa_min: float = Field(ge=-1.0, le=1.0)
