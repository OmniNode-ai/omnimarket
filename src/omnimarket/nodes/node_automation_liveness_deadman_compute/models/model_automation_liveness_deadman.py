# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of one dead-man evaluation (OMN-20807).

The dead-man is the watchdog in a narrow mode (plan 4.8): it watches the
primary's heartbeat and the broker, turns every opened and closed alarm episode
into one ledger row, and checks the daily canary episode. This compute is its
pure half. The effect that ticks it reads the heartbeat, the episode events and
the clock, and writes the ledger rows; nothing here reads a clock or a file.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
)

from omnimarket.models.liveness.model_automation_liveness import (
    ModelAutomationAlarmCleared,
    ModelAutomationAlarmDelivered,
    ModelAutomationAlarmRaised,
    ModelAutomationAlarmRecorded,
    NonEmptyStr,
    ProcessId,
)

#: The primary's heartbeat is silent once older than this (the bus silence bound).
PRIMARY_HEARTBEAT_BOUND_SECONDS = 10 * 60
#: Delivery budget the dead-man's own alarms carry (the watchdog's default).
DELIVERY_BUDGET_SECONDS = 120
#: The declared process whose daily injected episode proves the alarm path.
CANARY_PROCESS_ID = "liveness-canary"
#: The fixed hour (UTC) the canary injects its three no-work-with-demand runs.
CANARY_INJECTION_HOUR_UTC = 5
#: How long after the injection the episode may take to be delivered and recorded.
CANARY_RECEIPT_BUDGET_SECONDS = 15 * 60

OmnTicket = Annotated[str, StringConstraints(pattern=r"^OMN-\d+$")]


class EnumDeadmanPhase(StrEnum):
    OPENED = "opened"
    CLOSED = "closed"


class EnumDeadmanDeliveryRoute(StrEnum):
    #: The bus is up: delivery events and recorded events ride it as usual.
    BUS = "bus"
    #: The bus is unreachable: the alarm goes straight to the alert channel.
    DIRECT = "direct"


class ModelDeadmanEpisodeKey(BaseModel):
    """One ledger row's identity: an episode and the phase the row records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: UUID
    phase: EnumDeadmanPhase


class ModelDeadmanLedgerRow(BaseModel):
    """One FRICTION row owed to the ledger, stamped with the episode's own time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: ModelDeadmanEpisodeKey
    event_at: AwareDatetime
    process_id: ProcessId
    row: NonEmptyStr


class ModelDeadmanCanaryDay(BaseModel):
    """One day's canary episode and the events that prove the alarm path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: date
    #: True once the receipt budget after the injection has passed.
    judged: bool
    episode_id: UUID | None = None
    opened: bool = False
    delivered: bool = False
    recorded: bool = False
    closed: bool = False


class ModelAutomationLivenessDeadmanRequest(BaseModel):
    """Facts the dead-man tick has read; ``now`` is the only clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    #: Where the primary runs; the host a PRIMARY_SILENT alarm names.
    primary_host: NonEmptyStr
    #: Where the canary is emitted; the host an ALARM_PATH_BROKEN alarm names.
    canary_host: NonEmptyStr
    primary_last_heartbeat_at: AwareDatetime | None = None
    primary_heartbeat_bound_seconds: PositiveInt = PRIMARY_HEARTBEAT_BOUND_SECONDS
    #: Whether this host can reach the broker; False selects the direct route.
    broker_reachable: bool = True
    delivery_budget_seconds: PositiveInt = DELIVERY_BUDGET_SECONDS
    #: Alarm episodes consumed since the last tick (a replay is harmless).
    opened: tuple[ModelAutomationAlarmRaised, ...] = ()
    closed: tuple[ModelAutomationAlarmCleared, ...] = ()
    #: Delivery and recorded events the canary check reads.
    delivered: tuple[ModelAutomationAlarmDelivered, ...] = ()
    recorded: tuple[ModelAutomationAlarmRecorded, ...] = ()
    #: Rows the effect's store already holds; none of these is written again.
    already_recorded: tuple[ModelDeadmanEpisodeKey, ...] = ()
    ledger_ticket: OmnTicket = "OMN-20769"
    #: The days whose canary episode this tick judges.
    canary_dates: tuple[date, ...] = ()
    canary_injection_hour_utc: int = Field(CANARY_INJECTION_HOUR_UTC, ge=0, le=23)
    canary_receipt_budget_seconds: PositiveInt = CANARY_RECEIPT_BUDGET_SECONDS


class ModelAutomationLivenessDeadmanResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    delivery_route: EnumDeadmanDeliveryRoute
    #: Alarms the dead-man owns; the watchdog effect delivers them.
    alarms_raised: tuple[ModelAutomationAlarmRaised, ...] = ()
    #: Rows to append, oldest event first; none repeats an already recorded key.
    ledger_rows: tuple[ModelDeadmanLedgerRow, ...] = ()
    canary: tuple[ModelDeadmanCanaryDay, ...] = ()


__all__ = [
    "CANARY_INJECTION_HOUR_UTC",
    "CANARY_PROCESS_ID",
    "CANARY_RECEIPT_BUDGET_SECONDS",
    "DELIVERY_BUDGET_SECONDS",
    "PRIMARY_HEARTBEAT_BOUND_SECONDS",
    "EnumDeadmanDeliveryRoute",
    "EnumDeadmanPhase",
    "ModelAutomationLivenessDeadmanRequest",
    "ModelAutomationLivenessDeadmanResult",
    "ModelDeadmanCanaryDay",
    "ModelDeadmanEpisodeKey",
    "ModelDeadmanLedgerRow",
    "OmnTicket",
]
