# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Types of the stop escalation: the request, the one escalation row, the alarm's receipt and the result.

A stop that is not cleared after the reducer contract's ``stale_after_hours`` gets exactly one
escalation row, keyed by the stop, and an alarm whose delivery receipt is recorded on that row.
The stop itself is never changed: escalating does not clear it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.stop_gate import ModelStopRecord


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumStopEscalationOutcome(StrEnum):
    """What one escalation attempt did."""

    ESCALATED = "escalated"  # this call wrote the row and the alarm was delivered
    ALREADY_ESCALATED = (
        "already_escalated"  # the row and its receipt were there; nothing written
    )
    ALARM_REDELIVERED = "alarm_redelivered"  # the row was there without a receipt; the alarm now has one
    NOT_STALE = "not_stale"  # the stop is younger than stale_after_hours
    NOT_OPEN = (
        "not_open"  # the stop is in a terminal state; there is nothing to escalate
    )
    ALARM_FAILED = "alarm_failed"  # the row stands and no receipt came back; a replay retries the alarm


class ModelAlarmReceipt(_Frozen):
    """What the alarm channel returned for one delivered alarm."""

    delivery_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    delivered_at: datetime


class ModelStopEscalationRow(_Frozen):
    """The one escalation row of one stop; ``row_id`` is derived from the stop id, so a replay finds it."""

    row_id: str = Field(min_length=1)
    stop_id: str
    stop_class: str
    scope_kind: str
    scope_key: str
    stopped_at: datetime
    escalated_at: datetime
    stale_after_hours: int = Field(gt=0)
    receipt: ModelAlarmReceipt | None = None


class ModelStopEscalationRequest(_Frozen):
    """One stop as the queue holds it and the clock reading to judge it at."""

    stop: ModelStopRecord
    now: datetime


class ModelStopEscalationResult(_Frozen):
    """The outcome, the stop exactly as it came in (still open), the row and the receipt, if any."""

    outcome: EnumStopEscalationOutcome
    stop: ModelStopRecord
    row: ModelStopEscalationRow | None = None
    receipt: ModelAlarmReceipt | None = None
    detail: str = ""
