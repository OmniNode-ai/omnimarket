# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-state alarm (OMN-19998, 17a; plan figure W23).

``handle(ModelLabStateAlarmRequest) -> ModelLabStateAlarmResult``. A lab source that is not WORKING raises one alarm,
edge-triggered: it is raised when the source enters the bad state and not again until it has been WORKING. The alarm
goes to the primary channel and needs a delivery receipt read back within ``readback_s`` (5 minutes); a send that is
refused, or a receipt still not delivered at the deadline, sends it to the second channel as well. The alarm clears
only when the source reads WORKING. Every run stamps a heartbeat, so a silent alarm path can be told from a quiet one.

An open alarm with no delivery receipt is sent again every ``renotify_s`` seconds, on the primary channel and, once
escalated, on the second, until a send is read back as delivered. A send still pending inside the read-back window is
waited for and not repeated. An alarm with a delivered send never repeats. A retry replaces the earlier undelivered
send of the same channel, so a channel that stays down does not grow the state.

The channels are injected (in-memory by default, a real backend through the overlay); this handler names none.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from omnimarket.models.floor_17a import (
    ModelAlarm,
    ModelAlarmChannelDef,
    ModelAlarmSend,
    ModelAlarmState,
    ModelLabSourceVerdict,
    ModelLabStateAlarmRequest,
    ModelLabStateAlarmResult,
    ModelOpenAlarm,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.channels import (
    AlarmSendRefusedError,
    ProtocolAlarmChannel,
)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


class HandlerLabStateAlarm:
    def __init__(self, channels: Mapping[str, ProtocolAlarmChannel]) -> None:
        self._channels = dict(channels)

    def handle(self, request: ModelLabStateAlarmRequest) -> ModelLabStateAlarmResult:
        now, overlay = request.now, request.overlay
        by_role = {c.role: c for c in overlay.channels}
        primary, second = by_role["primary"], by_role["second"]
        verdicts = {v.id: v for v in request.verdicts}
        open_alarms = dict(request.state.open)
        raised: list[str] = []
        cleared: list[str] = []
        escalated: list[str] = []

        for source_id in list(open_alarms):
            verdict = verdicts.get(source_id)
            if verdict is not None and verdict.state == "WORKING":
                del open_alarms[source_id]
                cleared.append(source_id)

        for source_id, verdict in verdicts.items():
            if verdict.state != "WORKING" and source_id not in open_alarms:
                alarm = self._alarm(verdict, now, request.is_test)
                send = self._send(primary, alarm, now)
                open_alarms[source_id] = ModelOpenAlarm(
                    alarm=alarm, sends=(send,), last_notified_at=now
                )
                raised.append(source_id)

        for source_id, current in list(open_alarms.items()):
            updated = self._settle(
                current, primary, second, now, overlay.readback_s, overlay.renotify_s
            )
            if updated.escalated and not current.escalated:
                escalated.append(source_id)
            open_alarms[source_id] = updated

        undelivered = tuple(
            sorted(
                sid
                for sid, oa in open_alarms.items()
                if not any(s.delivery == "delivered" for s in oa.sends)
            )
        )
        return ModelLabStateAlarmResult(
            state=ModelAlarmState(open=open_alarms, heartbeat_at=now),
            raised=tuple(raised),
            escalated=tuple(escalated),
            cleared=tuple(cleared),
            undelivered=undelivered,
        )

    @staticmethod
    def _alarm(
        verdict: ModelLabSourceVerdict, now: datetime, is_test: bool
    ) -> ModelAlarm:
        return ModelAlarm(
            alarm_id=f"{verdict.id}@{_stamp(now)}",
            source_id=verdict.id,
            title=f"{verdict.title}: {verdict.state}",
            detail=verdict.reason,
            raised_at=now,
            is_test=is_test,
        )

    def _send(
        self, channel: ModelAlarmChannelDef, alarm: ModelAlarm, now: datetime
    ) -> ModelAlarmSend:
        try:
            receipt = self._channels[channel.id].send(alarm, now)
        except AlarmSendRefusedError as refused:
            return ModelAlarmSend(
                alarm_id=alarm.alarm_id,
                channel=channel.id,
                role=channel.role,
                receipt_id=None,
                sent_at=now,
                delivery="failed",
                detail=str(refused),
            )
        return ModelAlarmSend(
            alarm_id=alarm.alarm_id,
            channel=channel.id,
            role=channel.role,
            receipt_id=receipt,
            sent_at=now,
            delivery="pending",
        )

    def _settle(
        self,
        current: ModelOpenAlarm,
        primary: ModelAlarmChannelDef,
        second: ModelAlarmChannelDef,
        now: datetime,
        readback_s: int,
        renotify_s: int,
    ) -> ModelOpenAlarm:
        sends: list[ModelAlarmSend] = []
        for send in current.sends:
            if send.delivery == "pending" and send.receipt_id is not None:
                channel = self._channels[send.channel]
                state = channel.read_receipt(send.receipt_id, now)
                if state == "delivered":
                    send = send.model_copy(
                        update={"delivery": "delivered", "read_back_at": now}
                    )
                elif state == "failed":
                    send = send.model_copy(
                        update={"delivery": "failed", "detail": "read-back says failed"}
                    )
            sends.append(send)
        escalated = current.escalated
        primary_sends = [s for s in sends if s.role == "primary"]
        primary_ok = any(s.delivery == "delivered" for s in primary_sends)
        primary_late = any(
            s.delivery == "pending" and (now - s.sent_at).total_seconds() >= readback_s
            for s in primary_sends
        )
        primary_failed = bool(primary_sends) and all(
            s.delivery == "failed" for s in primary_sends
        )
        notified_at = current.last_notified_at
        if not escalated and not primary_ok and (primary_late or primary_failed):
            sends.append(self._send(second, current.alarm, now))
            escalated = True
        elif escalated and self._retry_due(
            sends, now, readback_s, renotify_s, notified_at
        ):
            for target in (primary, second):
                sends = [s for s in sends if s.channel != target.id]
                sends.append(self._send(target, current.alarm, now))
            notified_at = now
        return current.model_copy(
            update={
                "sends": tuple(sends),
                "escalated": escalated,
                "last_notified_at": notified_at,
            }
        )

    @staticmethod
    def _retry_due(
        sends: list[ModelAlarmSend],
        now: datetime,
        readback_s: int,
        renotify_s: int,
        last_notified_at: datetime,
    ) -> bool:
        """An alarm with no delivered send, none still inside its read-back window, and a quiet ``renotify_s``."""
        if any(s.delivery == "delivered" for s in sends):
            return False
        if any(
            s.delivery == "pending" and (now - s.sent_at).total_seconds() < readback_s
            for s in sends
        ):
            return False
        return (now - last_notified_at).total_seconds() >= renotify_s
