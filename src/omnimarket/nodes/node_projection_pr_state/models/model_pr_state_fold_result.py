# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One complete replacement row and its database timestamp."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.events.pr_state import (
    ModelPrStateObservedEvent,
)


class ModelPrStateFoldResult(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    event: ModelPrStateObservedEvent
    observed_at: datetime

    @property
    def key(self) -> str:
        return f"{self.event.repo}#{self.event.pr_number}"
