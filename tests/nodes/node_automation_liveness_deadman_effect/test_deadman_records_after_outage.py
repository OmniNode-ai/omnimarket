# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Episodes opened during a bus outage are recorded in order (OMN-20807, AC4)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessEvent,
    EnumWatchdogSelftest,
    automation_liveness_topics,
)
from omnimarket.nodes.node_automation_liveness_deadman_compute.handlers.handler_automation_liveness_deadman import (
    HandlerAutomationLivenessDeadman,
)
from omnimarket.nodes.node_automation_liveness_deadman_compute.models.model_automation_liveness_deadman import (
    ModelAutomationLivenessDeadmanRequest,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.models.model_deadman_record import (
    ModelDeadmanRecordRequest,
)
from tests.nodes.node_automation_liveness_deadman_effect.support import (
    T0,
    FakeLedger,
    RecordingSink,
    episode_id,
    make_effect,
    raised,
)

pytestmark = pytest.mark.unit

BACK = T0 + timedelta(minutes=30)


def _burst():
    """Opened at +1m, +3m and +5m, delivered to the dead-man in a shuffled burst."""
    return (
        raised("c-late", T0 + timedelta(minutes=5)),
        raised("a-early", T0 + timedelta(minutes=1)),
        raised("b-mid", T0 + timedelta(minutes=3)),
    )


def _rows(now=BACK, **kw):
    return (
        HandlerAutomationLivenessDeadman()
        .handle(
            ModelAutomationLivenessDeadmanRequest(
                now=now,
                primary_host="host-p",
                canary_host="host-p",
                primary_last_heartbeat_at=now,
                **kw,
            )
        )
        .ledger_rows
    )


def _record(handler, rows, now=BACK):
    return handler.handle(
        ModelDeadmanRecordRequest(
            now=now, host="host-d", process_started_at=T0, consumed_offset=7, rows=rows
        )
    )


def test_deadman_records_after_outage_rows_land_in_opening_order(
    tmp_path: Path,
) -> None:
    ledger = FakeLedger()
    out = _record(make_effect(tmp_path, ledger), _rows(opened=_burst()))
    assert [r.episode_id for r in out.recorded] == [
        episode_id("a-early"),
        episode_id("b-mid"),
        episode_id("c-late"),
    ]
    # Each row carries the time the episode opened, not the time the bus came back.
    assert [line.split(" | ")[0] for line in ledger.lines] == [
        "2026-10-12T08:01:00Z",
        "2026-10-12T08:03:00Z",
        "2026-10-12T08:05:00Z",
    ]


def test_deadman_records_after_outage_a_down_ledger_keeps_the_order_for_the_next_tick(
    tmp_path: Path,
) -> None:
    ledger = FakeLedger()
    handler = make_effect(tmp_path, ledger)
    ledger.refuse_with = "ledger host unreachable"
    first = _record(handler, _rows(opened=_burst()))
    assert first.recorded == ()
    assert [k.episode_id for k in first.backlog] == [
        episode_id("a-early"),
        episode_id("b-mid"),
        episode_id("c-late"),
    ]
    assert first.heartbeat.outbox_backlog == 3
    assert ledger.appends == 1  # stops at the first failure, never skips ahead

    ledger.refuse_with = None
    second = _record(
        handler, first_rows := _rows(opened=_burst()), BACK + timedelta(minutes=1)
    )
    assert first_rows
    assert [r.episode_id for r in second.recorded] == [
        episode_id("a-early"),
        episode_id("b-mid"),
        episode_id("c-late"),
    ]
    assert second.backlog == ()
    assert second.heartbeat.outbox_backlog == 0


def test_deadman_records_after_outage_the_recorded_events_and_heartbeat_ride_the_bus(
    tmp_path: Path,
) -> None:
    sink = RecordingSink()
    out = _record(make_effect(tmp_path, FakeLedger(), sink), _rows(opened=_burst()))
    topics = automation_liveness_topics()
    recorded_topic = topics[EnumAutomationLivenessEvent.ALARM_RECORDED]
    heartbeat_topic = topics[EnumAutomationLivenessEvent.HEARTBEAT]
    assert [t for t, _, _ in sink.published] == [recorded_topic] * 3 + [heartbeat_topic]
    assert out.publish_failures == 0
    beat = out.heartbeat
    assert beat.process_id == "watchdog/deadman"
    assert beat.selftest is EnumWatchdogSelftest.PASS
    assert beat.consumed_offset == 7
    assert beat.outbox_backlog == 0
    assert beat.progress_counter == 3
    assert beat.last_ledger_write_at == BACK


def test_deadman_records_after_outage_a_bus_still_down_counts_refusals_but_records(
    tmp_path: Path,
) -> None:
    sink = RecordingSink()
    sink.refuse = True
    ledger = FakeLedger()
    out = _record(make_effect(tmp_path, ledger, sink), _rows(opened=_burst()))
    assert len(ledger.lines) == 3
    assert out.publish_failures == 4  # three recorded events and the heartbeat
