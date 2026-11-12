# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What an alarm channel sender reports; shared by the watchdog and the lab alarm delivery."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.liveness.model_automation_liveness import NonEmptyStr


class ModelAlarmSendReceipt(BaseModel):
    """What the alarm sender reports: a Slack message timestamp or a failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    delivered: bool
    message_ref: NonEmptyStr | None = None
    failure: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _receipt_or_failure(self) -> Self:
        if self.delivered and (self.message_ref is None or self.failure is not None):
            raise ValueError("a delivered alarm carries its message_ref and no failure")
        if not self.delivered and (
            self.failure is None or self.message_ref is not None
        ):
            raise ValueError(
                "an undelivered alarm carries its failure and no message_ref"
            )
        return self


__all__ = ["ModelAlarmSendReceipt"]
