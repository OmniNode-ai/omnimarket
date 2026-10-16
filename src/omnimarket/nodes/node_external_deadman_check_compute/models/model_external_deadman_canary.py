# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The day's canary episode as a leg's mirror reports it (OMN-20808)."""

from __future__ import annotations

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict


class ModelExternalDeadmanCanary(BaseModel):
    """Injection time and receipt deadline travel with the state, so a day whose injection has
    not happened yet, or whose receipt budget is still open, is not a failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    injection_at: AwareDatetime
    receipt_deadline: AwareDatetime
    delivered: bool
    recorded: bool

    def missing(self, now: datetime) -> tuple[str, ...]:
        """The events still absent once the receipt deadline has passed; empty before then."""
        if now <= self.receipt_deadline:
            return ()
        return tuple(
            name
            for name, present in (
                ("delivered", self.delivered),
                ("recorded", self.recorded),
            )
            if not present
        )
