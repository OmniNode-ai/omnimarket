# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fakes and builders for the watchdog effect tests (OMN-20806)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

from omnibase_core.enums.enum_liveness_state import EnumLivenessState

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAlarmSeverity,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    ModelAutomationAlarmRaised,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.handler_automation_liveness_watchdog_effect import (
    HandlerAutomationLivenessWatchdogEffect,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.outbox_store import (
    OutboxStore,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.models.model_watchdog_delivery import (
    ModelAlarmSendReceipt,
    ModelWatchdogDeliveryRequest,
    ModelWatchdogDeliveryResult,
)

T0 = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
NAMESPACE = UUID("6d2f0b1e-0c1a-4d7e-9a55-2c0f6f3a1b10")


class SimulatedCrash(BaseException):
    """A process death: not an Exception, so no handler may swallow it."""


def episode_id(name: str) -> UUID:
    return uuid5(NAMESPACE, name)


def high_alarm(
    name: str = "lab-fill", *, opened_at: datetime = T0, due_seconds: int = 120
) -> ModelAutomationAlarmRaised:
    return ModelAutomationAlarmRaised(
        episode_id=episode_id(name),
        process_id=name,
        host="host-a",
        verdict=EnumAutomationLivenessVerdict.MISSED,
        state=EnumLivenessState.STALE,
        reason=EnumAutomationLivenessReason.NO_RUN_IN_MAX_SILENCE,
        severity=EnumAlarmSeverity.HIGH,
        opened_at=opened_at,
        delivery_due_at=opened_at + timedelta(seconds=due_seconds),
        evidence_ref=f"watchdog:{name}@host-a",
        action=f"diagnose:{name}",
    )


def low_alarm(
    name: str = "nightly", *, opened_at: datetime
) -> ModelAutomationAlarmRaised:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = int((opened_at - epoch).total_seconds())
    digest = epoch + timedelta(seconds=(elapsed // 21600 + 1) * 21600)
    return ModelAutomationAlarmRaised(
        episode_id=episode_id(name),
        process_id=name,
        host="host-a",
        verdict=EnumAutomationLivenessVerdict.FAILED,
        state=EnumLivenessState.RED,
        reason=EnumAutomationLivenessReason.RUN_FAILED,
        severity=EnumAlarmSeverity.LOW,
        opened_at=opened_at,
        delivery_due_at=digest + timedelta(seconds=120),
        evidence_ref=f"watchdog:{name}@host-a",
        action=f"diagnose:{name}",
    )


class FakeChannel:
    """The alert channel: sender and history reader over one list of messages."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.refuse_with: str | None = None
        self.history_unreadable = False
        self.attempts = 0
        self.crash_after_accept = False

    def send(
        self, *, episode_id: UUID, category: str, text: str
    ) -> ModelAlarmSendReceipt:
        self.attempts += 1
        if self.refuse_with is not None:
            return ModelAlarmSendReceipt(delivered=False, failure=self.refuse_with)
        ref = f"1760000{len(self.messages):03d}.000100"
        self.messages.append((ref, text))
        if self.crash_after_accept:
            self.crash_after_accept = False
            raise SimulatedCrash("died after Slack accepted the message")
        return ModelAlarmSendReceipt(delivered=True, message_ref=ref)

    def find_message_ref(self, episode_id: UUID) -> str | None:
        if self.history_unreadable:
            raise OSError("conversations.history unreachable")
        return next(
            (ref for ref, text in self.messages if str(episode_id) in text), None
        )

    def count_for(self, episode_id: UUID) -> int:
        return sum(1 for _, text in self.messages if str(episode_id) in text)


class RecordingSink:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object], str]] = []

    def publish(
        self, topic: str, payload: dict[str, object], *, key: str, event_id: str
    ) -> None:
        self.published.append((topic, payload, key))


class FakeEscalation:
    def __init__(self, log: list[str] | None = None) -> None:
        self.calls: list[UUID] = []
        self.log = log if log is not None else []

    def escalate(self, *, episode_id: UUID, text: str) -> str:
        self.calls.append(episode_id)
        self.log.append("escalate")
        return f"issue#{len(self.calls)}"


def make_handler(
    tmp_path: Path,
    channel: FakeChannel,
    escalation: FakeEscalation | None = None,
    *,
    store: OutboxStore | None = None,
) -> HandlerAutomationLivenessWatchdogEffect:
    return HandlerAutomationLivenessWatchdogEffect(
        store=store or OutboxStore(tmp_path / "watchdog.sqlite"),
        sender=channel,
        history=channel,
        escalation=escalation or FakeEscalation(),
    )


def tick(
    handler: HandlerAutomationLivenessWatchdogEffect,
    now: datetime,
    *alarms: ModelAutomationAlarmRaised,
) -> ModelWatchdogDeliveryResult:
    return handler.handle(ModelWatchdogDeliveryRequest(now=now, alarms_raised=alarms))
