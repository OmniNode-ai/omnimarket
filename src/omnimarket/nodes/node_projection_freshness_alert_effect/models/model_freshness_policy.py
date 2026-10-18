# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required cadence and renotification policy; no threshold defaults."""

from pydantic import BaseModel, ConfigDict, Field


class ModelFreshnessPolicy(BaseModel):
    """Thresholds loaded exclusively from freshness_policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    check_interval_seconds: int = Field(..., gt=0, strict=True)
    renotify_after_seconds: int = Field(..., gt=0, strict=True)
