# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stop escalation effect (OMN-22941, AC2): one row, one alarm with a receipt, and the stop stays open."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.handlers.rules_stop_gate import load_andon_stop_contract
from omnimarket.models.model_stop_escalation import (
    EnumStopEscalationOutcome,
    ModelAlarmReceipt,
    ModelStopEscalationRequest,
    ModelStopEscalationRow,
)
from omnimarket.models.stop_gate import ModelStopRecord, ModelStopScope
from omnimarket.nodes.node_stop_escalation_effect.handlers.adapters_stop_escalation import (
    InMemoryStopAlarm,
    InMemoryStopEscalationRows,
)
from omnimarket.nodes.node_stop_escalation_effect.handlers.handler_stop_escalation import (
    HandlerStopEscalation,
    escalation_row_id,
)
from omnimarket.protocols.protocol_stop_escalation import (
    ProtocolStopAlarm,
    ProtocolStopEscalationRows,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
_, CONFIG = load_andon_stop_contract()
STALE = timedelta(hours=CONFIG.stale_after_hours)


def stop(state: str = "swarming", stop_id: str = "stop-1") -> ModelStopRecord:
    return ModelStopRecord(
        id=stop_id,
        cls="engine-policy-breach",
        scope=ModelStopScope(kind="engine", key="example-engine"),
        state=state,
        stopped_at=T0,
    )


class RefusingAlarm:
    def __init__(self) -> None:
        self.asked = 0

    def deliver(self, row: ModelStopEscalationRow) -> ModelAlarmReceipt:
        self.asked += 1
        raise ConnectionError("the alarm channel is unreachable")


def build(
    alarm: ProtocolStopAlarm | None = None,
) -> tuple[HandlerStopEscalation, InMemoryStopEscalationRows, InMemoryStopAlarm]:
    rows = InMemoryStopEscalationRows()
    channel = InMemoryStopAlarm("test-alarm", T0 + STALE)
    return (
        HandlerStopEscalation(rows, alarm if alarm is not None else channel),
        rows,
        channel,
    )


def ask(
    handler: HandlerStopEscalation, record: ModelStopRecord, after: timedelta
) -> object:
    return handler.handle(ModelStopEscalationRequest(stop=record, now=T0 + after))


def test_ports_are_satisfied_by_the_in_memory_adapters() -> None:
    assert isinstance(InMemoryStopEscalationRows(), ProtocolStopEscalationRows)
    assert isinstance(InMemoryStopAlarm("c", T0), ProtocolStopAlarm)


def test_escalate_once_a_stale_stop_gets_one_row_and_one_alarm_with_a_receipt() -> None:
    handler, rows, channel = build()
    record = stop()
    first = handler.handle(ModelStopEscalationRequest(stop=record, now=T0 + STALE))
    assert first.outcome is EnumStopEscalationOutcome.ESCALATED
    assert first.receipt is not None
    assert first.receipt.delivery_id
    assert first.row is not None
    assert first.row.row_id == escalation_row_id("stop-1")
    assert first.row.stale_after_hours == CONFIG.stale_after_hours
    assert first.row.receipt == first.receipt
    assert first.stop == record
    assert first.stop.state == "swarming"
    assert first.stop.cleared_at is None
    # the same stop judged again, now and later: no second row, no second alarm
    for later in (STALE, STALE + timedelta(hours=1), STALE * 3):
        again = handler.handle(ModelStopEscalationRequest(stop=record, now=T0 + later))
        assert again.outcome is EnumStopEscalationOutcome.ALREADY_ESCALATED
        assert again.receipt == first.receipt
    assert len(rows.all_rows()) == 1
    assert channel.deliveries == 1


def test_escalate_once_a_stop_younger_than_the_limit_gets_nothing() -> None:
    handler, rows, channel = build()
    young = handler.handle(
        ModelStopEscalationRequest(stop=stop(), now=T0 + STALE - timedelta(seconds=1))
    )
    assert young.outcome is EnumStopEscalationOutcome.NOT_STALE
    assert young.row is None
    assert rows.all_rows() == []
    assert channel.deliveries == 0


@pytest.mark.parametrize("state", ["cleared", "declared_false"])
def test_escalate_once_a_stop_that_ended_gets_nothing(state: str) -> None:
    handler, rows, channel = build()
    result = handler.handle(
        ModelStopEscalationRequest(stop=stop(state), now=T0 + STALE * 2)
    )
    assert result.outcome is EnumStopEscalationOutcome.NOT_OPEN
    assert rows.all_rows() == []
    assert channel.deliveries == 0


def test_escalate_once_each_stop_has_its_own_row() -> None:
    handler, rows, channel = build()
    for stop_id in ("stop-1", "stop-2"):
        handler.handle(
            ModelStopEscalationRequest(stop=stop(stop_id=stop_id), now=T0 + STALE)
        )
    assert sorted(r.stop_id for r in rows.all_rows()) == ["stop-1", "stop-2"]
    assert channel.deliveries == 2


def test_escalate_once_a_failed_alarm_keeps_the_row_and_a_replay_delivers_without_a_second_row() -> (
    None
):
    refusing = RefusingAlarm()
    handler, rows, _ = build(alarm=refusing)
    record = stop()
    failed = handler.handle(ModelStopEscalationRequest(stop=record, now=T0 + STALE))
    assert failed.outcome is EnumStopEscalationOutcome.ALARM_FAILED
    assert failed.receipt is None
    assert "ConnectionError" in failed.detail
    [row] = rows.all_rows()
    assert row.receipt is None
    working = InMemoryStopAlarm("test-alarm", T0 + STALE)
    replay = HandlerStopEscalation(rows, working).handle(
        ModelStopEscalationRequest(stop=record, now=T0 + STALE + timedelta(hours=1))
    )
    assert replay.outcome is EnumStopEscalationOutcome.ALARM_REDELIVERED
    assert replay.receipt is not None
    [only] = rows.all_rows()
    assert only.row_id == row.row_id
    assert only.receipt == replay.receipt
    assert working.deliveries == 1
