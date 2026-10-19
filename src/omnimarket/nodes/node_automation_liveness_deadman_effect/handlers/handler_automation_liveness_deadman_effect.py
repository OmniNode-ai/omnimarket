# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dead-man's ledger writes and heartbeat (OMN-20807, plan 4.7 and 4.8).

One tick takes the rows the dead-man compute returned, oldest first, and
appends each to the ledger exactly once: a key the store holds, or one the
ledger already carries (a crash between an append and the store), is recorded
as written and not appended again. The first refused or failed append stops the
tick, so the rows behind it stay in order in the backlog and the heartbeat's
``outbox_backlog`` says how many wait; the next tick resumes at that row.

Each row written becomes a recorded event with the ledger line. The dead-man's
heartbeat carries its consumed offset, its backlog, its last ledger write and
``selftest``: a planted stale primary heartbeat must raise PRIMARY_SILENT and a
fresh one must not, or the heartbeat says the self-test failed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessEvent,
    EnumAutomationLivenessVerdict,
    EnumWatchdogSelftest,
    ModelAutomationAlarmRecorded,
    ModelAutomationHeartbeat,
    automation_liveness_topics,
)
from omnimarket.models.liveness.model_automation_liveness_deadman import (
    ModelAutomationLivenessDeadmanRequest,
    ModelDeadmanEpisodeKey,
    ModelDeadmanLedgerRow,
)
from omnimarket.nodes.node_automation_liveness_deadman_compute.handlers.handler_automation_liveness_deadman import (
    HandlerAutomationLivenessDeadman,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.handlers.ledger_appender import (
    LedgerCommandAppender,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.handlers.protocols import (
    ProtocolLedgerAppender,
    UnwiredLedgerAppender,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.handlers.recorded_store import (
    RecordedStore,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.models.model_deadman_record import (
    DEADMAN_PROCESS_ID,
    ModelDeadmanRecordRequest,
    ModelDeadmanRecordResult,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.protocols import (
    ProtocolAlarmEventSink,
    UnwiredEventSink,
)

logger = logging.getLogger(__name__)

_PLANTED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def run_selftest() -> EnumWatchdogSelftest:
    """Plant a stale and a fresh primary heartbeat through the real compute."""
    compute = HandlerAutomationLivenessDeadman()

    def verdicts(last: datetime | None) -> set[EnumAutomationLivenessVerdict]:
        result = compute.handle(
            ModelAutomationLivenessDeadmanRequest(
                now=_PLANTED_NOW,
                primary_host="planted-host",
                canary_host="planted-host",
                primary_last_heartbeat_at=last,
            )
        )
        return {alarm.verdict for alarm in result.alarms_raised}

    stale = verdicts(_PLANTED_NOW - timedelta(hours=1))
    fresh = verdicts(_PLANTED_NOW - timedelta(seconds=30))
    ok = stale == {EnumAutomationLivenessVerdict.PRIMARY_SILENT} and not fresh
    return EnumWatchdogSelftest.PASS if ok else EnumWatchdogSelftest.FAIL


class HandlerAutomationLivenessDeadmanEffect:
    def __init__(
        self,
        *,
        store: RecordedStore | None = None,
        appender: ProtocolLedgerAppender | None = None,
        sink: ProtocolAlarmEventSink | None = None,
    ) -> None:
        self._store_override = store
        self._appender_override = appender
        self._sink: ProtocolAlarmEventSink = sink or UnwiredEventSink()

    def handle(self, request: ModelDeadmanRecordRequest) -> ModelDeadmanRecordResult:
        store = self._store_for(request)
        appender = self._appender_for(request)
        held = set(store.recorded_keys())
        recorded: list[ModelAutomationAlarmRecorded] = []
        skipped: list[ModelDeadmanEpisodeKey] = []
        backlog: list[ModelDeadmanEpisodeKey] = []
        failure: str | None = None

        for row in sorted(
            request.rows, key=lambda r: (r.event_at, str(r.key.episode_id))
        ):
            if failure is not None:
                backlog.append(row.key)
                continue
            if row.key in held:
                skipped.append(row.key)
                continue
            try:
                line = self._write(appender, row)
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                backlog.append(row.key)
                continue
            store.mark_recorded(row.key, line, request.now)
            held.add(row.key)
            recorded.append(
                ModelAutomationAlarmRecorded(
                    episode_id=row.key.episode_id,
                    recorded_at=request.now,
                    ledger_line=line,
                    recorded_by=DEADMAN_PROCESS_ID,
                )
            )

        heartbeat = self._heartbeat(request, store, len(backlog))
        publish_failures = self._publish(recorded, heartbeat)
        return ModelDeadmanRecordResult(
            now=request.now,
            recorded=tuple(recorded),
            skipped=tuple(skipped),
            backlog=tuple(backlog),
            failure=failure,
            publish_failures=publish_failures,
            heartbeat=heartbeat,
        )

    @staticmethod
    def _write(appender: ProtocolLedgerAppender, row: ModelDeadmanLedgerRow) -> str:
        """The line a row sits on: found in the ledger already, or appended now."""
        existing = appender.find_line(row.key)
        if existing is not None:
            return existing
        return appender.append(row.key, row.row)

    def _heartbeat(
        self, request: ModelDeadmanRecordRequest, store: RecordedStore, backlog: int
    ) -> ModelAutomationHeartbeat:
        last = store.last_recorded_at()
        return ModelAutomationHeartbeat(
            process_id=DEADMAN_PROCESS_ID,
            host=request.host,
            process_started_at=request.process_started_at,
            progress_counter=store.recorded_count(),
            last_progress_at=last,
            emitted_at=request.now,
            selftest=run_selftest(),
            consumed_offset=request.consumed_offset,
            outbox_backlog=backlog,
            last_ledger_write_at=last,
        )

    def _publish(
        self,
        recorded: list[ModelAutomationAlarmRecorded],
        heartbeat: ModelAutomationHeartbeat,
    ) -> int:
        """Publish each event on the seam's topic; returns the refusals."""
        topics = automation_liveness_topics()
        events = [
            (
                topics[EnumAutomationLivenessEvent.ALARM_RECORDED],
                event.model_dump(mode="json"),
                str(event.episode_id),
                f"{event.episode_id}:recorded:{event.ledger_line}",
            )
            for event in recorded
        ]
        events.append(
            (
                topics[EnumAutomationLivenessEvent.HEARTBEAT],
                heartbeat.model_dump(mode="json"),
                heartbeat.process_id,
                f"{heartbeat.process_id}:{heartbeat.emitted_at.isoformat()}",
            )
        )
        refused = 0
        for topic, payload, key, event_id in events:
            try:
                self._sink.publish(topic, payload, key=key, event_id=event_id)
            except Exception as exc:
                refused += 1
                logger.warning("dead-man event %s not published: %s", event_id, exc)
        return refused

    def _store_for(self, request: ModelDeadmanRecordRequest) -> RecordedStore:
        if self._store_override is not None:
            return self._store_override
        if request.store_path is None:
            raise ValueError("no recorded store: pass store_path or inject a store")
        return RecordedStore(request.store_path)

    def _appender_for(
        self, request: ModelDeadmanRecordRequest
    ) -> ProtocolLedgerAppender:
        if self._appender_override is not None:
            return self._appender_override
        if request.ledger_path is None or request.ledger_append_command is None:
            return UnwiredLedgerAppender()
        return LedgerCommandAppender(
            request.ledger_path, tuple(request.ledger_append_command)
        )


__all__ = ["HandlerAutomationLivenessDeadmanEffect", "run_selftest"]
