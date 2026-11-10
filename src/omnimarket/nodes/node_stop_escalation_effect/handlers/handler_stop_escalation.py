# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stop escalation effect: ``handle(ModelStopEscalationRequest) -> ModelStopEscalationResult``.

A stop still open at ``stale_after_hours`` (the number is the stop reducer contract's, read once)
gets exactly one escalation row, keyed by the stop id, and one alarm whose delivery receipt is
recorded on the row. A replay finds the row: with its receipt it writes nothing; without one it
asks the alarm again (the alarm is idempotent on the row id) and records the receipt, so a crash
between the row and the alarm never leaves a stop escalated without an alarm and never writes a
second row. The stop is returned as it came in: escalating does not clear it.
"""

from __future__ import annotations

from datetime import timedelta

from omnimarket.handlers.rules_stop_gate import load_andon_stop_contract
from omnimarket.models.model_stop_escalation import (
    EnumStopEscalationOutcome,
    ModelAlarmReceipt,
    ModelStopEscalationRequest,
    ModelStopEscalationResult,
    ModelStopEscalationRow,
)
from omnimarket.protocols.protocol_stop_escalation import (
    ProtocolStopAlarm,
    ProtocolStopEscalationRows,
)

ROW_ID_PREFIX = "stop-escalation:"


def escalation_row_id(stop_id: str) -> str:
    """The one row id of a stop's escalation."""
    return f"{ROW_ID_PREFIX}{stop_id}"


class HandlerStopEscalation:
    """One stop in, at most one escalation row and one alarm out."""

    def __init__(
        self,
        rows: ProtocolStopEscalationRows,
        alarm: ProtocolStopAlarm,
        *,
        stale_after_hours: int | None = None,
    ) -> None:
        self._rows = rows
        self._alarm = alarm
        fsm, config = load_andon_stop_contract()
        self._terminal = frozenset(fsm.terminal_states)
        self._stale_after_hours = (
            stale_after_hours
            if stale_after_hours is not None
            else config.stale_after_hours
        )

    def handle(self, request: ModelStopEscalationRequest) -> ModelStopEscalationResult:
        stop = request.stop
        if stop.state in self._terminal:
            return ModelStopEscalationResult(
                outcome=EnumStopEscalationOutcome.NOT_OPEN,
                stop=stop,
                detail=f"stop {stop.id} is {stop.state}",
            )
        waited = request.now - stop.stopped_at
        if waited < timedelta(hours=self._stale_after_hours):
            return ModelStopEscalationResult(
                outcome=EnumStopEscalationOutcome.NOT_STALE,
                stop=stop,
                detail=f"stop {stop.id} has waited {waited}, under {self._stale_after_hours}h",
            )
        row = ModelStopEscalationRow(
            row_id=escalation_row_id(stop.id),
            stop_id=stop.id,
            stop_class=stop.cls,
            scope_kind=stop.scope.kind,
            scope_key=stop.scope.key,
            stopped_at=stop.stopped_at,
            escalated_at=request.now,
            stale_after_hours=self._stale_after_hours,
        )
        wrote = self._rows.append_if_absent(row)
        durable = self._rows.read(row.row_id)
        if durable is None:
            return ModelStopEscalationResult(
                outcome=EnumStopEscalationOutcome.ALARM_FAILED,
                stop=stop,
                detail="the escalation row could not be read back; no alarm was sent",
            )
        if not wrote and durable.receipt is not None:
            return ModelStopEscalationResult(
                outcome=EnumStopEscalationOutcome.ALREADY_ESCALATED,
                stop=stop,
                row=durable,
                receipt=durable.receipt,
            )
        try:
            receipt: ModelAlarmReceipt = self._alarm.deliver(durable)
        except (
            Exception
        ) as exc:  # the channel's own fault is an answer; the row stands for a replay
            return ModelStopEscalationResult(
                outcome=EnumStopEscalationOutcome.ALARM_FAILED,
                stop=stop,
                row=durable,
                detail=f"alarm not delivered: {type(exc).__name__}",
            )
        self._rows.attach_receipt(durable.row_id, receipt)
        return ModelStopEscalationResult(
            outcome=(
                EnumStopEscalationOutcome.ESCALATED
                if wrote
                else EnumStopEscalationOutcome.ALARM_REDELIVERED
            ),
            stop=stop,
            row=durable.model_copy(update={"receipt": receipt}),
            receipt=receipt,
        )
