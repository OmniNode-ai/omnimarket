# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, outbox row and result of one watchdog delivery tick (OMN-20806).

One tick takes the alarm episodes the compute opened, writes them (and the
state the compute asked to carry) to the durable store in one transaction,
then delivers what is due. ``now`` is the only clock.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    model_validator,
)

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAlarmSeverity,
    EnumAutomationLivenessVerdict,
    ModelAutomationAlarmDelivered,
    ModelAutomationAlarmRaised,
    NonEmptyStr,
    ProcessId,
)

#: At most this many alarm messages an hour reach Slack; the rest ride one digest.
HOURLY_MESSAGE_CAP = 20


class EnumOutboxStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    #: A low-severity row replaced by a high-severity row of the same episode.
    SUPERSEDED = "superseded"


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


class ModelOutboxEntry(BaseModel):
    """One alarm message owed to the channel, as the store keeps it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: UUID
    severity: EnumAlarmSeverity
    process_id: ProcessId
    host: NonEmptyStr
    verdict: EnumAutomationLivenessVerdict
    text: NonEmptyStr
    opened_at: AwareDatetime
    #: High: when it opened. Low: the digest instant before the delivery budget.
    send_after: AwareDatetime
    delivery_due_at: AwareDatetime
    status: EnumOutboxStatus = EnumOutboxStatus.PENDING
    attempts: NonNegativeInt = 0
    last_failure: str | None = None
    message_ref: str | None = None
    delivered_at: AwareDatetime | None = None
    escalation_ref: str | None = None


class ModelWatchdogDeliveryRequest(BaseModel):
    """One tick: the episodes the compute raised and the state to carry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    #: The SQLite file holding the carried state and the outbox, when the
    #: handler was not given a store.
    store_path: Path | None = None
    #: The alert-channel shell library, when the handler was not given a sender.
    alert_channel_library: Path | None = None
    alarms_raised: tuple[ModelAutomationAlarmRaised, ...] = ()
    #: The compute's next state, opaque here; None leaves the stored state alone.
    carried_state: dict[str, object] | None = None
    #: The overlay's delivery budget; a low-severity row is sent this long before it is due.
    delivery_budget_seconds: PositiveInt = 120
    hourly_cap: PositiveInt = HOURLY_MESSAGE_CAP


class ModelWatchdogDeliveryResult(BaseModel):
    """Every delivery attempt of the tick, and what the store holds afterwards."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    #: One event per attempt, Slack or the external route, to publish.
    delivered: tuple[ModelAutomationAlarmDelivered, ...] = ()
    #: Delivered by finding the episode id already in the channel history.
    reconciled: tuple[UUID, ...] = ()
    #: Undelivered past the due time and sent again this tick.
    retried: tuple[UUID, ...] = ()
    escalated: tuple[UUID, ...] = ()
    #: Rows that rode the over-cap digest instead of a message each.
    digested: NonNegativeInt = 0
    #: Rows still owed to the channel after this tick.
    pending: NonNegativeInt = 0
    #: Delivery events the sink would not take; they stay in ``delivered``.
    publish_failures: NonNegativeInt = 0
    carried_state: dict[str, object] | None = Field(default=None)


__all__ = [
    "HOURLY_MESSAGE_CAP",
    "EnumOutboxStatus",
    "ModelAlarmSendReceipt",
    "ModelOutboxEntry",
    "ModelWatchdogDeliveryRequest",
    "ModelWatchdogDeliveryResult",
]
