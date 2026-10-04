# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Volume facts of the corpus the first calibrated run judged."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceCorpusFacts(BaseModel):
    """Volume facts of the corpus the first calibrated run judged."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1)
    judged_items: int = Field(ge=1)
    cells: int = Field(ge=1)
    events: int = Field(ge=1)
    volume_weighted_accept_rate: float = Field(ge=0.0, le=1.0)
    volume_terminal_ok_rate: float = Field(ge=0.0, le=1.0)
