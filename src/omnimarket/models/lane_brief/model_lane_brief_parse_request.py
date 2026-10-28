# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts for parsing one brief."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneBriefParseRequest(BaseModel):
    """The brief text and the time of the parse; nothing is configured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brief: str = Field(min_length=1)
    now: datetime
