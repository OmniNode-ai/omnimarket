# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-check calibration counts for one arm."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelGateCheckRecord(BaseModel):
    """Per-check calibration counts for one arm."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    arm: Literal["replayed", "recorded", "rubric"]
    catches: int = Field(ge=0)
    wrong_refusals: int = Field(ge=0)
    skips: int = Field(ge=0)
    skip_reasons: tuple[tuple[str, int], ...] = ()
