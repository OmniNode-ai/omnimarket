# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reader that feeds the install-silence judge, over a fake projection and a fake ledger (OMN-26658, AC4)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.nodes.node_hook_install_emission_compute.models import (
    ModelHookInstallObservedEvent,
    ModelHookInstallRequiredKind,
)
from omnimarket.nodes.node_hook_install_silence_alert_effect.handlers import (
    HandlerHookInstallSilenceAlert,
)
from omnimarket.nodes.node_hook_install_silence_alert_effect.models import (
    EnumSilenceOutcome,
    ModelHookInstallSilenceSettings,
    ModelHookInstallSilenceTrigger,
    ModelOwnedInstall,
)
from omnimarket.nodes.node_work_progress_alert_effect.models import ModelLedgerRow

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
KINDS = (
    ModelHookInstallRequiredKind(
        kind="session_start", hook_event_names=("SessionStart",)
    ),
)
SETTINGS = ModelHookInstallSilenceSettings(
    installs=(
        ModelOwnedInstall(host="h101", tool="claude"),
        ModelOwnedInstall(host="h101", tool="codex"),
    ),
    required_kinds=KINDS,
    lookback_minutes=240,
    quiet_minutes=30,
    check_interval_seconds=300,
)


class FakeEvents:
    def __init__(self, events: Sequence[ModelHookInstallObservedEvent] = ()) -> None:
        self._events = tuple(events)
        self.since: datetime | None = None

    def read(self, since: datetime) -> Sequence[ModelHookInstallObservedEvent]:
        self.since = since
        return self._events


class UnreadableEvents:
    def read(self, since: datetime) -> Sequence[ModelHookInstallObservedEvent]:
        raise RuntimeError("claude_hook_events: connection refused")


class FakeLedger:
    def __init__(self, rows: Sequence[ModelLedgerRow] = ()) -> None:
        self._rows = tuple(rows)

    def read(self, claimed_since: datetime) -> Sequence[ModelLedgerRow]:
        return self._rows


class RecordingAlarm:
    def __init__(self) -> None:
        self.requests: list[ModelLabAlarmDeliveryRequest] = []

    def handle(
        self, request: ModelLabAlarmDeliveryRequest
    ) -> ModelLabAlarmDeliveryResult:
        self.requests.append(request)
        return ModelLabAlarmDeliveryResult(
            correlation_id=request.correlation_id,
            condition=request.condition,
            outcome=EnumDeliveryOutcome.DELIVERED
            if request.alarms
            else EnumDeliveryOutcome.NOTHING_NEW,
            deliveries=(),
            cleared=(),
            retried=(),
            state_kept=True,
        )


def _lane_row(n: int, tool: str, host: str, minutes_ago: int) -> ModelLedgerRow:
    ts = NOW - timedelta(minutes=minutes_ago)
    return ModelLedgerRow(
        row_id=f"r{n}",
        row_ts=ts,
        row_type="CLAIM",
        row_lane=f"lane-{n}",
        raw_row=f"{ts:%Y-%m-%dT%H:%M:%SZ} | CLAIM | lane=lane-{n} | actor={tool}:session | host={host}",
    )


def _event(tool: str, minutes_ago: int) -> ModelHookInstallObservedEvent:
    return ModelHookInstallObservedEvent(
        host="h101",
        actor=tool,  # type: ignore[arg-type]
        hook_event_name="SessionStart",
        emitted_at=NOW - timedelta(minutes=minutes_ago),
    )


def _handler(
    events: object, ledger: FakeLedger, alarm: RecordingAlarm
) -> HandlerHookInstallSilenceAlert:
    return HandlerHookInstallSilenceAlert(
        settings=SETTINGS,
        events_reader=events,  # type: ignore[arg-type]
        ledger_reader=ledger,
        alarm=alarm,
        clock=lambda: NOW,
    )


def test_silent_codex_alerts_by_tool_while_claude_emits() -> None:
    alarm = RecordingAlarm()
    ledger = FakeLedger(
        (_lane_row(1, "claude", "h101", 90), _lane_row(2, "codex", "h101", 90))
    )
    result = _handler(FakeEvents((_event("claude", 80),)), ledger, alarm).handle(
        ModelHookInstallSilenceTrigger()
    )
    assert result.outcome is EnumSilenceOutcome.ALERTED
    assert [(a.host, a.tool) for a in result.alerts] == [("h101", "codex")]
    (sent,) = alarm.requests
    assert sent.outcome is EnumDeliveryCondition.ALARM
    assert [a.subject for a in sent.alarms] == ["h101/codex"]
    assert "h101" in sent.evidence
    assert "codex" in sent.evidence
    assert sent.evidence == result.alert_text


def test_known_good_stays_quiet() -> None:
    alarm = RecordingAlarm()
    ledger = FakeLedger(
        (_lane_row(1, "claude", "h101", 90), _lane_row(2, "codex", "h101", 90))
    )
    events = FakeEvents((_event("claude", 80), _event("codex", 80)))
    result = _handler(events, ledger, alarm).handle(ModelHookInstallSilenceTrigger())
    assert result.outcome is EnumSilenceOutcome.QUIET
    assert result.alerts == ()
    (sent,) = alarm.requests
    assert sent.outcome is EnumDeliveryCondition.OK
    assert sent.alarms == ()
    assert events.since == NOW - timedelta(minutes=240)


def test_unreadable_projection_alerts_as_unreadable() -> None:
    alarm = RecordingAlarm()
    ledger = FakeLedger((_lane_row(1, "codex", "h101", 90),))
    result = _handler(UnreadableEvents(), ledger, alarm).handle(
        ModelHookInstallSilenceTrigger()
    )
    assert result.outcome is EnumSilenceOutcome.UNREADABLE
    assert "connection refused" in result.reason
    (sent,) = alarm.requests
    assert sent.outcome is EnumDeliveryCondition.ALARM
    assert [a.subject for a in sent.alarms] == ["claude_hook_events"]
    assert "unreadable" in sent.alarms[0].detail


def test_install_with_no_session_stays_quiet() -> None:
    alarm = RecordingAlarm()
    result = _handler(FakeEvents(), FakeLedger(), alarm).handle(
        ModelHookInstallSilenceTrigger()
    )
    assert result.outcome is EnumSilenceOutcome.QUIET


class _Cursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object) -> None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _Conn:
    closed = 0

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def cursor(self) -> _Cursor:
        return _Cursor(self._rows)

    def close(self) -> None:
        self.closed = 1


def _postgres(rows: list[tuple[object, ...]]) -> object:
    from omnimarket.nodes.node_hook_install_silence_alert_effect.handlers.postgres_events_reader import (
        PostgresHookInstallEventsReader,
    )

    reader = PostgresHookInstallEventsReader(dsn="postgresql://fake")
    reader._conn = _Conn(rows)
    return reader


def test_postgres_reader_maps_rows() -> None:
    reader = _postgres([("h101", "claude", "SessionStart", NOW)])
    (event,) = reader.read(NOW)  # type: ignore[attr-defined]
    assert (event.host, event.actor) == ("h101", "claude")


def test_postgres_reader_refuses_rows_with_no_actor_instead_of_reading_every_install_silent() -> (
    None
):
    import pytest

    reader = _postgres([("h101", None, "SessionStart", NOW)])
    with pytest.raises(RuntimeError, match="carry no actor"):
        reader.read(NOW)  # type: ignore[attr-defined]
