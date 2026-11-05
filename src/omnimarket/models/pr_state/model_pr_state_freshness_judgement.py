# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict on whether one projection row is current."""

from pydantic import BaseModel, ConfigDict


class ModelPrStateFreshnessJudgement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fresh: bool
    row_age_seconds: float
