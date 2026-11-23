# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1: nothing is sent for a night that is not terminal; a terminal night sends collect then publish for one date, once."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from tests.nodes.node_test_selector_nightly_sender_scheduled_effect.support import (
    AFTER_NIGHTLY,
    NODE,
    SLOT,
    FakeRuns,
    FakeSender,
    handler,
    run_view,
    tick,
)

pytestmark = pytest.mark.unit


def test_sender_order_and_once_a_night_that_is_not_terminal_sends_nothing(
    tmp_path: Path,
) -> None:
    sender = FakeSender()
    h = handler(tmp_path, FakeRuns((run_view(status="in_progress"),)), sender)
    event = h.handle(tick(AFTER_NIGHTLY))
    assert event is not None
    assert (event.outcome, event.node_name, event.job_id) == (
        "completed",
        NODE,
        "test_selector_nightly_sender",
    )
    assert "slot=2026-11-22 waiting" in event.detail
    assert "in_progress" in event.detail
    assert sender.sent == []


def test_sender_order_and_once_a_terminal_night_sends_collect_then_publish_once(
    tmp_path: Path,
) -> None:
    sender = FakeSender()
    runs = FakeRuns((run_view(),))
    h = handler(tmp_path, runs, sender)
    event = h.handle(tick(AFTER_NIGHTLY))
    assert event is not None
    assert event.outcome == "completed"
    assert event.detail == (
        "slot=2026-11-22 sent collect_nightly=collected publish_attribution_record=published"
    )
    assert sender.sent == [
        ("collect_nightly", SLOT),
        ("publish_attribution_record", SLOT),
    ]
    # the publish command carries the collect answer and the policy values, not literals of the sender
    publish = sender.publish_commands[0]
    assert publish.collect_result == {"status": "collected", "slot_date": "2026-11-22"}
    assert (
        publish.selector_version,
        publish.policy_version,
        publish.stage_entry_counted,
    ) == (
        "1.0.0",
        "abc123def456",
        1,
    )
    # a second tick, past the interval, for the same date sends nothing
    assert h.handle(tick(AFTER_NIGHTLY + timedelta(seconds=900))) is None
    assert len(sender.sent) == 2


def test_sender_order_and_once_a_waiting_night_is_sent_when_it_finishes(
    tmp_path: Path,
) -> None:
    sender = FakeSender()
    runs = FakeRuns((run_view(status="in_progress"),))
    h = handler(tmp_path, runs, sender)
    waiting = h.handle(tick(AFTER_NIGHTLY))
    assert waiting is not None
    assert "waiting" in waiting.detail
    runs.runs = (run_view(status="completed"),)
    sent = h.handle(tick(AFTER_NIGHTLY + timedelta(seconds=900)))
    assert sent is not None
    assert sent.outcome == "completed"
    assert [name for name, _ in sender.sent] == [
        "collect_nightly",
        "publish_attribution_record",
    ]


def test_sender_order_and_once_a_restart_with_the_state_file_sends_nothing(
    tmp_path: Path,
) -> None:
    first = FakeSender()
    assert handler(tmp_path, FakeRuns((run_view(),)), first).handle(tick(AFTER_NIGHTLY))
    assert len(first.sent) == 2
    restarted = FakeSender()
    fresh = handler(tmp_path, FakeRuns((run_view(),)), restarted)
    assert fresh.handle(tick(AFTER_NIGHTLY + timedelta(seconds=60))) is None
    assert restarted.sent == []


def test_sender_order_and_once_a_finished_run_inside_the_settle_margin_waits(
    tmp_path: Path,
) -> None:
    sender = FakeSender()
    just_done = run_view(updated=AFTER_NIGHTLY - timedelta(seconds=30))
    event = handler(tmp_path, FakeRuns((just_done,)), sender).handle(
        tick(AFTER_NIGHTLY)
    )
    assert event is not None
    assert "settle margin" in event.detail
    assert sender.sent == []


def test_sender_order_and_once_before_the_earliest_check_time_the_previous_slot_is_followed(
    tmp_path: Path,
) -> None:
    sender = FakeSender()
    h = handler(tmp_path, FakeRuns((run_view(),)), sender)
    early = AFTER_NIGHTLY.replace(hour=2, minute=45)
    event = h.handle(tick(early))
    assert event is not None
    # 02:45 is before the 03:15 check time: the slot followed is the previous date, which has no run in the listing
    assert "slot=2026-11-21" in event.detail
    assert sender.sent == []
