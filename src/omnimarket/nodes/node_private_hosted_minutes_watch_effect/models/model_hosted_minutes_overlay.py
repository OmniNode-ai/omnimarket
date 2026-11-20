# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment values of the watch: included minutes and the alert fraction."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelHostedMinutesOverlay(BaseModel):
    """Read from the node overlay; neither field has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    included_minutes: int = Field(gt=0)
    alert_fraction: float = Field(gt=0, le=1)
