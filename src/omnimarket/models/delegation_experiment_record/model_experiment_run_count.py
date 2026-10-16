# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The number of counted runs of one accepted experiment."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelExperimentRunCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    run_count: int = Field(ge=0)
