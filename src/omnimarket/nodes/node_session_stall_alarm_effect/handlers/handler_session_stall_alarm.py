# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the session stall alarm: read the rows, judge them, deliver the verdict (OMN-25405).

The judge is node_session_stall_alarm_compute's. Delivery is node_lab_alarm_deliver_effect's, edge-triggered: a stall is
posted once to the alert channel and recorded as one ledger row, not again until a tick reads OK; a transcript that
cannot be read is INDETERMINATE, which neither starts nor ends an episode.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.models.session_stall import (
    EnumSessionStallOutcome,
    ModelSessionStallRequest,
    ModelSessionStallTickRequest,
    ModelSessionStallTickResult,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_session_stall_alarm_compute.handlers.handler_session_stall_judge import (
    HandlerSessionStallJudge,
)
from omnimarket.nodes.node_session_stall_alarm_effect.overlay import (
    ModelSessionStallOverlay,
)
from omnimarket.nodes.node_session_stall_alarm_effect.protocols import (
    ProtocolTranscriptRows,
    TranscriptUnreadableError,
)

CONDITION = "session_stall"


class HandlerSessionStallAlarm:
    def __init__(
        self,
        *,
        overlay: ModelSessionStallOverlay,
        reader: ProtocolTranscriptRows,
        judge: HandlerSessionStallJudge,
        delivery: HandlerLabAlarmDeliver,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._reader = reader
        self._judge = judge
        self._delivery = delivery
        self._clock = clock

    def handle(
        self, request: ModelSessionStallTickRequest
    ) -> ModelSessionStallTickResult:
        now = self._clock()
        try:
            rows = self._reader.read(now)
        except TranscriptUnreadableError as exc:
            delivery = self._delivery.handle(
                ModelLabAlarmDeliveryRequest(
                    correlation_id=request.correlation_id,
                    condition=CONDITION,
                    outcome=EnumDeliveryCondition.INDETERMINATE,
                    evidence=str(exc),
                    write=request.write,
                )
            )
            return ModelSessionStallTickResult(
                correlation_id=request.correlation_id,
                outcome=EnumSessionStallOutcome.INDETERMINATE,
                verdict=None,
                delivery=delivery,
                failure=str(exc),
            )
        verdict = self._judge.handle(
            ModelSessionStallRequest(
                now=now,
                rows=rows,
                tick_declared=self._overlay.tick_declared,
                dequeue_after_seconds=self._overlay.dequeue_after_seconds,
                task_after_seconds=self._overlay.task_after_seconds,
            )
        )
        if verdict.stalled:
            delivery_request = ModelLabAlarmDeliveryRequest(
                correlation_id=request.correlation_id,
                condition=CONDITION,
                outcome=EnumDeliveryCondition.ALARM,
                alarms=(
                    ModelLabAlarm(
                        condition=CONDITION,
                        subject=self._overlay.subject,
                        detail=verdict.detail,
                    ),
                ),
                evidence=verdict.evidence,
                write=request.write,
            )
        else:
            delivery_request = ModelLabAlarmDeliveryRequest(
                correlation_id=request.correlation_id,
                condition=CONDITION,
                outcome=EnumDeliveryCondition.OK,
                write=request.write,
            )
        return ModelSessionStallTickResult(
            correlation_id=request.correlation_id,
            outcome=(
                EnumSessionStallOutcome.STALLED
                if verdict.stalled
                else EnumSessionStallOutcome.OK
            ),
            verdict=verdict,
            delivery=self._delivery.handle(delivery_request),
        )


__all__ = ["CONDITION", "HandlerSessionStallAlarm"]
