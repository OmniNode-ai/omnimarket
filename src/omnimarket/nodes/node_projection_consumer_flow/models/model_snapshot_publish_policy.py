# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared policy for consumer-flow snapshot publishes."""

from pydantic import BaseModel, ConfigDict, Field


class ModelSnapshotPublishPolicy(BaseModel):
    """Publish on verdict changes or after the event-time refresh interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict_columns: tuple[str, ...] = Field(..., min_length=1)
    refresh_interval_seconds: int = Field(..., gt=0)


__all__ = ["ModelSnapshotPublishPolicy"]
