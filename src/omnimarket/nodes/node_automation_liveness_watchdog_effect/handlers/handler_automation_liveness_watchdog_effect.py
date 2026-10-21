# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable delivery of the watchdog's alarm episodes (OMN-20806).

One tick decides, then delivers. Deciding commits the new outbox rows and the
carried state in one transaction. Delivery marks an attempt before it sends, so
a row with an attempt may already be in the channel: before any re-send the
channel history is read for the episode id, and a row found there is recorded
as delivered instead of posted again. A crash after deciding, after Slack
accepted, or after the outbox recorded therefore loses no alarm and costs at
most one duplicate (only when the history itself cannot be read).

A row not delivered by its due time is sent again and, if that fails too,
escalated once to the route outside Slack. A missing credential is a failed
attempt, never a delivery. At most ``hourly_cap`` messages an hour reach the
channel; the rest ride one digest message that names the count.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from uuid import UUID

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAlarmDeliveryRoute,
    EnumAlarmSeverity,
    EnumAutomationLivenessEvent,
    ModelAutomationAlarmDelivered,
    ModelAutomationAlarmRaised,
    automation_liveness_topics,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.alert_channel_sender import (
    AlertChannelAlarmSender,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.outbox_store import (
    OutboxStore,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.protocols import (
    ProtocolAlarmEventSink,
    ProtocolAlarmSender,
    ProtocolChannelHistory,
    ProtocolEscalationRoute,
    UnwiredChannelHistory,
    UnwiredEscalationRoute,
    UnwiredEventSink,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.models.model_watchdog_delivery import (
    ModelAlarmSendReceipt,
    ModelOutboxEntry,
    ModelWatchdogDeliveryRequest,
    ModelWatchdogDeliveryResult,
)

logger = logging.getLogger(__name__)

ALARM_CATEGORY = "automation-liveness"


class _Tick:
    """What one tick did, collected for the result."""

    def __init__(self) -> None:
        self.events: list[ModelAutomationAlarmDelivered] = []
        self.reconciled: list[UUID] = []
        self.retried: list[UUID] = []
        self.escalated: list[UUID] = []
        self.digested = 0


class HandlerAutomationLivenessWatchdogEffect:
    def __init__(
        self,
        *,
        store: OutboxStore | None = None,
        sender: ProtocolAlarmSender | None = None,
        history: ProtocolChannelHistory | None = None,
        escalation: ProtocolEscalationRoute | None = None,
        sink: ProtocolAlarmEventSink | None = None,
    ) -> None:
        self._sink: ProtocolAlarmEventSink = sink or UnwiredEventSink()
        self._store_override = store
        self._sender_override = sender
        self._history: ProtocolChannelHistory = history or UnwiredChannelHistory()
        self._escalation: ProtocolEscalationRoute = (
            escalation or UnwiredEscalationRoute()
        )
        # Bound for the length of one tick, from the injected ports or the request.
        self._store: OutboxStore
        self._sender: ProtocolAlarmSender

    def handle(
        self, request: ModelWatchdogDeliveryRequest
    ) -> ModelWatchdogDeliveryResult:
        now = request.now
        self._store, self._sender = self._ports_for(request)
        self._store.commit_decision(
            [self._queued(alarm, request) for alarm in request.alarms_raised],
            request.carried_state,
        )

        tick = _Tick()
        room = max(
            request.hourly_cap - self._store.delivered_since(now - timedelta(hours=1)),
            0,
        )
        overflow: list[ModelOutboxEntry] = []
        for entry in self._store.pending():
            if now < entry.send_after:
                continue
            if self._reconcile(entry, now, tick):
                continue
            if room <= 0:
                overflow.append(entry)
                continue
            room -= 1
            self._send_one(entry, now, tick)
        if overflow:
            self._send_digest(overflow, request.hourly_cap, now, tick)

        publish_failures = self._publish(tick.events)
        return ModelWatchdogDeliveryResult(
            now=now,
            delivered=tuple(tick.events),
            reconciled=tuple(tick.reconciled),
            retried=tuple(tick.retried),
            escalated=tuple(tick.escalated),
            digested=tick.digested,
            pending=len(self._store.pending()),
            publish_failures=publish_failures,
            carried_state=self._store.load_state(),
        )

    def _publish(self, events: list[ModelAutomationAlarmDelivered]) -> int:
        """Publish each delivery event on the seam's topic; returns the refusals."""
        topic = automation_liveness_topics()[
            EnumAutomationLivenessEvent.ALARM_DELIVERED
        ]
        refused = 0
        for event in events:
            try:
                self._sink.publish(
                    topic,
                    event.model_dump(mode="json"),
                    key=str(event.episode_id),
                    event_id=(
                        f"{event.episode_id}:{event.route.value}"
                        f":{event.attempted_at.isoformat()}"
                    ),
                )
            except Exception as exc:
                refused += 1
                logger.warning(
                    "delivery event for episode %s not published: %s",
                    event.episode_id,
                    exc,
                )
        return refused

    def _ports_for(
        self, request: ModelWatchdogDeliveryRequest
    ) -> tuple[OutboxStore, ProtocolAlarmSender]:
        store = self._store_override
        if store is None:
            if request.store_path is None:
                raise ValueError("no outbox store: pass store_path or inject a store")
            store = OutboxStore(request.store_path)
        sender = self._sender_override
        if sender is None:
            if request.alert_channel_library is None:
                raise ValueError(
                    "no alarm sender: pass alert_channel_library or inject a sender"
                )
            sender = AlertChannelAlarmSender(request.alert_channel_library)
        return store, sender

    # ------------------------------------------------------------------ deciding

    @staticmethod
    def _queued(
        alarm: ModelAutomationAlarmRaised, request: ModelWatchdogDeliveryRequest
    ) -> ModelOutboxEntry:
        send_after = (
            alarm.opened_at
            if alarm.severity is EnumAlarmSeverity.HIGH
            else alarm.delivery_due_at
            - timedelta(seconds=request.delivery_budget_seconds)
        )
        text = (
            f"[{alarm.severity.value.upper()}] {alarm.verdict.value} "
            f"{alarm.process_id}@{alarm.host} ({alarm.reason.value}) "
            f"| action={alarm.action} | episode={alarm.episode_id}"
        )
        if alarm.detail:
            text += f" | {alarm.detail}"
        return ModelOutboxEntry(
            episode_id=alarm.episode_id,
            severity=alarm.severity,
            process_id=alarm.process_id,
            host=alarm.host,
            verdict=alarm.verdict,
            text=text,
            opened_at=alarm.opened_at,
            send_after=send_after,
            delivery_due_at=alarm.delivery_due_at,
        )

    # ----------------------------------------------------------------- delivering

    def _reconcile(self, entry: ModelOutboxEntry, now: datetime, tick: _Tick) -> bool:
        """Record a row whose episode id is already in the channel; True if found."""
        if entry.attempts == 0:
            return False
        try:
            ref = self._history.find_message_ref(entry.episode_id)
        except Exception as exc:
            logger.warning(
                "channel history unreadable for episode %s: %s", entry.episode_id, exc
            )
            return False
        if ref is None:
            return False
        self._store.record_delivery(entry, ref, now)
        tick.reconciled.append(entry.episode_id)
        tick.events.append(self._delivered_event(entry.episode_id, now, ref))
        return True

    def _send_one(self, entry: ModelOutboxEntry, now: datetime, tick: _Tick) -> None:
        if entry.attempts > 0 and now > entry.delivery_due_at:
            tick.retried.append(entry.episode_id)
        marked = self._store.mark_attempt(entry)
        receipt = self._post(entry.episode_id, entry.text)
        self._settle(marked, receipt, now, tick)

    def _send_digest(
        self,
        entries: list[ModelOutboxEntry],
        cap: int,
        now: datetime,
        tick: _Tick,
    ) -> None:
        listing = "; ".join(
            f"{e.verdict.value} {e.process_id}@{e.host} episode={e.episode_id}"
            for e in entries
        )
        text = (
            f"[DIGEST] {len(entries)} further automation-liveness alarms held by "
            f"the {cap}/hour cap: {listing}"
        )
        marked = [self._store.mark_attempt(e) for e in entries]
        receipt = self._post(entries[0].episode_id, text)
        for entry in marked:
            self._settle(entry, receipt, now, tick)
        if receipt.delivered:
            tick.digested += len(entries)

    def _post(self, episode_id: UUID, text: str) -> ModelAlarmSendReceipt:
        try:
            return self._sender.send(
                episode_id=episode_id, category=ALARM_CATEGORY, text=text
            )
        except Exception as exc:
            return ModelAlarmSendReceipt(
                delivered=False, failure=f"sender raised {type(exc).__name__}: {exc}"
            )

    def _settle(
        self,
        entry: ModelOutboxEntry,
        receipt: ModelAlarmSendReceipt,
        now: datetime,
        tick: _Tick,
    ) -> None:
        if receipt.delivered and receipt.message_ref is not None:
            self._store.record_delivery(entry, receipt.message_ref, now)
            tick.events.append(
                self._delivered_event(entry.episode_id, now, receipt.message_ref)
            )
            return
        failure = receipt.failure or "alarm sender reported no delivery"
        failed = self._store.record_attempt_failure(entry, failure)
        tick.events.append(
            ModelAutomationAlarmDelivered(
                episode_id=entry.episode_id,
                route=EnumAlarmDeliveryRoute.SLACK,
                attempted_at=now,
                delivered=False,
                failure=failure,
            )
        )
        if now > failed.delivery_due_at and failed.escalation_ref is None:
            self._escalate(failed, now, tick)

    def _escalate(self, entry: ModelOutboxEntry, now: datetime, tick: _Tick) -> None:
        try:
            ref = self._escalation.escalate(
                episode_id=entry.episode_id, text=entry.text
            )
        except Exception as exc:
            tick.events.append(
                ModelAutomationAlarmDelivered(
                    episode_id=entry.episode_id,
                    route=EnumAlarmDeliveryRoute.GITHUB_ISSUE,
                    attempted_at=now,
                    delivered=False,
                    failure=f"escalation raised {type(exc).__name__}: {exc}",
                )
            )
            return
        self._store.record_escalation(entry, ref)
        tick.escalated.append(entry.episode_id)
        tick.events.append(
            self._delivered_event(
                entry.episode_id, now, ref, EnumAlarmDeliveryRoute.GITHUB_ISSUE
            )
        )

    @staticmethod
    def _delivered_event(
        episode_id: UUID,
        now: datetime,
        ref: str,
        route: EnumAlarmDeliveryRoute = EnumAlarmDeliveryRoute.SLACK,
    ) -> ModelAutomationAlarmDelivered:
        return ModelAutomationAlarmDelivered(
            episode_id=episode_id,
            route=route,
            attempted_at=now,
            delivered=True,
            message_ref=ref,
        )


__all__ = ["ALARM_CATEGORY", "HandlerAutomationLivenessWatchdogEffect"]
