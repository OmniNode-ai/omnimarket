# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A raised session stall is delivered once and recorded as one ledger row (OMN-25405, criterion 2).

The tick reads the transcript rows, has the judge decide, and hands the verdict to the lab alarm's delivery
(edge-triggered: one channel post and one ledger row per episode, an OK ends the episode).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.lab_alarm_delivery import EnumDeliveryOutcome
from omnimarket.models.session_stall import (
    EnumQueueOperation,
    EnumSessionStallOutcome,
    ModelQueueRow,
    ModelSessionStallTickRequest,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay
from omnimarket.nodes.node_session_stall_alarm_compute.handlers.handler_session_stall_judge import (
    HandlerSessionStallJudge,
)
from omnimarket.nodes.node_session_stall_alarm_effect.handlers.handler_session_stall_alarm import (
    HandlerSessionStallAlarm,
)
from omnimarket.nodes.node_session_stall_alarm_effect.overlay import (
    ModelSessionStallOverlay,
)
from omnimarket.nodes.node_session_stall_alarm_effect.protocols import (
    TranscriptUnreadableError,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
T0 = datetime(2026, 11, 17, 7, 58, 57, tzinfo=UTC)


def _overlay_text(node: str) -> dict[str, object]:
    document = yaml.safe_load(
        (ROOT / node / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(document, dict)
    return document


class _Reader:
    """The transcript's rows as the test plants them; ``fail`` makes the read refuse."""

    def __init__(self, rows: tuple[ModelQueueRow, ...]) -> None:
        self.rows = rows
        self.fail = False

    def read(self, now: datetime) -> tuple[ModelQueueRow, ...]:
        if self.fail:
            raise TranscriptUnreadableError("planted: the transcript is not readable")
        return self.rows


class _Rig:
    def __init__(self, rows: tuple[ModelQueueRow, ...]) -> None:
        self.channel = InMemoryAlarmChannel()
        self.ledger = InMemoryAlarmLedger()
        self.reader = _Reader(rows)
        self.now = T0 + timedelta(minutes=20)
        delivery = HandlerLabAlarmDeliver(
            overlay=ModelDeliveryOverlay.model_validate(
                _overlay_text("node_lab_alarm_deliver_effect")
            ),
            channel=self.channel,
            ledger=self.ledger,
            state=InMemoryDeliveryState(),
            clock=lambda: self.now,
        )
        self.handler = HandlerSessionStallAlarm(
            overlay=ModelSessionStallOverlay.model_validate(
                _overlay_text("node_session_stall_alarm_effect")
            ),
            reader=self.reader,
            judge=HandlerSessionStallJudge(),
            delivery=delivery,
            clock=lambda: self.now,
        )

    def tick(self) -> EnumSessionStallOutcome:
        result = self.handler.handle(
            ModelSessionStallTickRequest(correlation_id=f"tick-{self.now.isoformat()}")
        )
        self.last = result
        self.now += timedelta(minutes=5)
        return result.outcome


def _planted() -> tuple[ModelQueueRow, ...]:
    return (ModelQueueRow(at=T0, operation=EnumQueueOperation.ENQUEUE),)


def _drained(minutes: int = 28) -> tuple[ModelQueueRow, ...]:
    """The planted enqueue taken by a dequeue ``minutes`` after it (rows are never later than the tick)."""
    return (
        *_planted(),
        ModelQueueRow(
            at=T0 + timedelta(minutes=minutes), operation=EnumQueueOperation.DEQUEUE
        ),
    )


def test_session_stall_delivery_one_delivery_and_one_row_across_three_ticks() -> None:
    rig = _Rig(_planted())
    assert [rig.tick(), rig.tick(), rig.tick()] == [EnumSessionStallOutcome.STALLED] * 3
    assert len(rig.channel.posts) == 1
    assert len(rig.ledger.rows) == 1
    _, text = rig.channel.posts[0]
    assert "session_stall" in text
    assert "orchestrating-session" in text


def test_session_stall_delivery_the_ticks_after_the_first_deliver_nothing_new() -> None:
    rig = _Rig(_planted())
    rig.tick()
    assert rig.last.delivery.outcome is EnumDeliveryOutcome.DELIVERED
    rig.tick()
    assert rig.last.delivery.outcome is EnumDeliveryOutcome.NOTHING_NEW
    rig.tick()
    assert rig.last.delivery.outcome is EnumDeliveryOutcome.NOTHING_NEW


def test_session_stall_delivery_is_not_resent_until_it_clears() -> None:
    rig = _Rig(_planted())
    rig.tick()
    rig.tick()
    rig.reader.rows = _drained()
    assert rig.tick() is EnumSessionStallOutcome.OK
    assert rig.last.delivery.cleared == ("session_stall:orchestrating-session",)
    # A new stall after the clear is a new episode: it is posted again; the ledger window holds the second row back.
    rig.reader.rows = (
        *_drained(),
        ModelQueueRow(
            at=T0 + timedelta(minutes=10), operation=EnumQueueOperation.ENQUEUE
        ),
    )
    assert rig.tick() is EnumSessionStallOutcome.STALLED
    assert len(rig.channel.posts) == 2
    assert len(rig.ledger.rows) == 1


def test_session_stall_delivery_a_healthy_session_delivers_nothing() -> None:
    rig = _Rig(_drained(1))
    assert rig.tick() is EnumSessionStallOutcome.OK
    assert rig.channel.posts == []
    assert rig.ledger.rows == []


def test_session_stall_delivery_an_unreadable_transcript_neither_alarms_nor_clears() -> (
    None
):
    rig = _Rig(_planted())
    rig.tick()
    rig.reader.fail = True
    assert rig.tick() is EnumSessionStallOutcome.INDETERMINATE
    assert rig.last.verdict is None
    assert rig.last.delivery.cleared == ()
    # The episode is still open, so the stall that reads again is not posted a second time.
    rig.reader.fail = False
    assert rig.tick() is EnumSessionStallOutcome.STALLED
    assert len(rig.channel.posts) == 1


def test_session_stall_delivery_a_probe_delivers_nothing() -> None:
    rig = _Rig(_planted())
    result = rig.handler.handle(
        ModelSessionStallTickRequest(correlation_id="probe", write=False)
    )
    assert result.outcome is EnumSessionStallOutcome.STALLED
    assert result.delivery.outcome is EnumDeliveryOutcome.SKIPPED
    assert rig.channel.posts == []
    assert rig.ledger.rows == []
