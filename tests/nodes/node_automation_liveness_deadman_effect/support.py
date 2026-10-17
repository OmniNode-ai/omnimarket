# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders and fakes for the dead-man tests (OMN-20807)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

from omnibase_core.enums.enum_liveness_state import EnumLivenessState

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAlarmSeverity,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    ModelAutomationAlarmCleared,
    ModelAutomationAlarmDelivered,
    ModelAutomationAlarmRaised,
    ModelAutomationAlarmRecorded,
)
from omnimarket.nodes.node_automation_liveness_deadman_compute.models.model_automation_liveness_deadman import (
    CANARY_PROCESS_ID,
    EnumDeadmanPhase,
    ModelDeadmanEpisodeKey,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.handlers.handler_automation_liveness_deadman_effect import (
    HandlerAutomationLivenessDeadmanEffect,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.handlers.recorded_store import (
    RecordedStore,
)

T0 = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
NAMESPACE = UUID("0b7a3c1e-52d4-4c8a-8f0e-7d1b2a9c4e61")


def episode_id(name: str) -> UUID:
    return uuid5(NAMESPACE, name)


def raised(
    name: str, opened_at: datetime, *, process_id: str | None = None
) -> ModelAutomationAlarmRaised:
    return ModelAutomationAlarmRaised(
        episode_id=episode_id(name),
        process_id=process_id or name,
        host="host-a",
        verdict=EnumAutomationLivenessVerdict.MISSED,
        state=EnumLivenessState.STALE,
        reason=EnumAutomationLivenessReason.NO_RUN_IN_MAX_SILENCE,
        severity=EnumAlarmSeverity.HIGH,
        opened_at=opened_at,
        delivery_due_at=opened_at + timedelta(seconds=120),
        evidence_ref=f"watchdog:{name}@host-a",
        action=f"diagnose:{name}",
    )


def cleared(
    name: str, opened_at: datetime, at: datetime
) -> ModelAutomationAlarmCleared:
    return ModelAutomationAlarmCleared(
        episode_id=episode_id(name),
        process_id=name,
        host="host-a",
        verdict=EnumAutomationLivenessVerdict.MISSED,
        opened_at=opened_at,
        cleared_at=at,
    )


def delivered(name: str, at: datetime) -> ModelAutomationAlarmDelivered:
    return ModelAutomationAlarmDelivered(
        episode_id=episode_id(name),
        route="slack",
        attempted_at=at,
        delivered=True,
        message_ref="1760000000.000100",
    )


def recorded_event(name: str, at: datetime) -> ModelAutomationAlarmRecorded:
    return ModelAutomationAlarmRecorded(
        episode_id=episode_id(name),
        recorded_at=at,
        ledger_line="ROLLING_WORK_LEDGER.md:1",
        recorded_by="watchdog/deadman",
    )


def canary_name(day: str) -> str:
    return f"{CANARY_PROCESS_ID}-{day}"


def canary_raised(day: str, opened_at: datetime) -> ModelAutomationAlarmRaised:
    return raised(canary_name(day), opened_at, process_id=CANARY_PROCESS_ID)


def key(name: str, phase: EnumDeadmanPhase) -> ModelDeadmanEpisodeKey:
    return ModelDeadmanEpisodeKey(episode_id=episode_id(name), phase=phase)


class FakeLedger:
    """A ledger file in memory: the appender, with its crash and refusal switches."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.refuse_with: str | None = None
        self.crash_after_append = False
        self.appends = 0

    def find_line(self, key: ModelDeadmanEpisodeKey) -> str | None:
        for number, line in enumerate(self.lines, start=1):
            if f"| episode={key.episode_id} " in line and (
                f"| phase={key.phase.value} " in line
            ):
                return f"ledger:{number}"
        return None

    def append(self, key: ModelDeadmanEpisodeKey, row: str) -> str:
        self.appends += 1
        if self.refuse_with is not None:
            raise RuntimeError(self.refuse_with)
        self.lines.append(row)
        if self.crash_after_append:
            self.crash_after_append = False
            raise SimulatedCrash("died after the ledger took the row")
        return f"ledger:{len(self.lines)}"


class SimulatedCrash(BaseException):
    """A process death: not an Exception, so no handler may swallow it."""


class RecordingSink:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object], str]] = []
        self.refuse = False

    def publish(
        self, topic: str, payload: dict[str, object], *, key: str, event_id: str
    ) -> None:
        if self.refuse:
            raise ConnectionError("broker unreachable")
        self.published.append((topic, payload, key))


def make_effect(
    tmp_path: Path, ledger: FakeLedger, sink: RecordingSink | None = None
) -> HandlerAutomationLivenessDeadmanEffect:
    return HandlerAutomationLivenessDeadmanEffect(
        store=RecordedStore(tmp_path / "deadman.sqlite"),
        appender=ledger,
        sink=sink or RecordingSink(),
    )
