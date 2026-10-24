# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The timer supplies optional correlation context for one evaluation."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelCaptureWatchTrigger(BaseModel):
    """A oneshot tick carries no policy or observation overrides."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: UUID | None = None
