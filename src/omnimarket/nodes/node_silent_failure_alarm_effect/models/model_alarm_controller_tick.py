# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one drain watchdog tick observed, as typed values."""

from __future__ import annotations

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict


class ModelAlarmControllerTick(BaseModel):
    """The landing controller's last receipt and whether the one before it was UNKNOWN."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: AwareDatetime
    mode: str
    status: str
    refusal: str | None
    detail: str
    previous_unknown: bool = False
    completed: AwareDatetime | None = None
    running_since: AwareDatetime | None = None
    """Start stamp of a tick in flight, from the heartbeat, set only when later than ts."""

    def last_activity(self) -> datetime:
        """The latest receipt stamp, receipt file write time or tick in flight start stamp."""
        return max(
            stamp
            for stamp in (self.ts, self.completed, self.running_since)
            if stamp is not None
        )
