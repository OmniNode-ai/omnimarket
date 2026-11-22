# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: a collect answer that is not collected sends no publish; a night not terminal after the lag is a named missed night."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender import (
    EnumCollectStatus,
    EnumPublishStatus,
)
from tests.nodes.node_test_selector_nightly_sender_scheduled_effect.support import (
    AFTER_NIGHTLY,
    SLOT_START,
    FakeRuns,
    FakeSender,
    handler,
    run_view,
    tick,
)

pytestmark = pytest.mark.unit

AFTER_LAG = SLOT_START + timedelta(hours=6, minutes=1)


@pytest.mark.parametrize(
    "status",
    [
        EnumCollectStatus.WAITING,
        EnumCollectStatus.NO_RUN,
        EnumCollectStatus.READ_FAILED,
        EnumCollectStatus.DISABLED,
    ],
)
def test_sender_not_collected_or_missed_a_not_collected_answer_sends_no_publish(
    tmp_path: Path, status: EnumCollectStatus
) -> None:
    sender = FakeSender(collect_status=status)
    h = handler(tmp_path, FakeRuns((run_view(),)), sender)
    event = h.handle(tick(AFTER_NIGHTLY))
    assert event is not None
    assert event.outcome == "failed"
    assert "slot=2026-11-22" in event.detail
    assert f"collect_nightly answered {status.value}" in event.detail
    assert "publish_attribution_record was not sent" in event.detail
    assert [name for name, _ in sender.sent] == ["collect_nightly"]
    # before the lag the next interval tries again
    sender.collect_status = EnumCollectStatus.COLLECTED
    retried = h.handle(tick(AFTER_NIGHTLY + timedelta(seconds=900)))
    assert retried is not None
    assert retried.outcome == "completed"


def test_sender_not_collected_or_missed_a_collect_with_no_answer_is_a_failed_run(
    tmp_path: Path,
) -> None:
    sender = FakeSender(collect_error=True)
    event = handler(tmp_path, FakeRuns((run_view(),)), sender).handle(
        tick(AFTER_NIGHTLY)
    )
    assert event is not None
    assert event.outcome == "failed"
    assert "slot=2026-11-22" in event.detail
    assert "collect_nightly was not answered" in event.detail
    assert [name for name, _ in sender.sent] == ["collect_nightly"]


def test_sender_not_collected_or_missed_a_publish_that_is_not_published_is_not_settled(
    tmp_path: Path,
) -> None:
    sender = FakeSender(publish_status=EnumPublishStatus.WRITE_PENDING)
    h = handler(tmp_path, FakeRuns((run_view(),)), sender)
    event = h.handle(tick(AFTER_NIGHTLY))
    assert event is not None
    assert event.outcome == "failed"
    assert "publish_attribution_record answered write_pending" in event.detail
    sender.publish_status = EnumPublishStatus.ALREADY_PUBLISHED
    retried = h.handle(tick(AFTER_NIGHTLY + timedelta(seconds=900)))
    assert retried is not None
    assert retried.outcome == "completed"
    assert "publish_attribution_record=already_published" in retried.detail


def test_sender_not_collected_or_missed_a_night_not_terminal_after_the_lag_is_a_missed_night(
    tmp_path: Path,
) -> None:
    sender = FakeSender()
    runs = FakeRuns((run_view(status="in_progress"),))
    h = handler(tmp_path, runs, sender)
    event = h.handle(tick(AFTER_LAG))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail.startswith("missed night slot=2026-11-22:")
    assert "in_progress" in event.detail
    assert sender.sent == []
    # recorded once: a later tick for the same date is quiet, never a silent loop of events
    assert h.handle(tick(AFTER_LAG + timedelta(seconds=900))) is None


def test_sender_not_collected_or_missed_a_night_with_no_run_after_the_lag_is_a_missed_night(
    tmp_path: Path,
) -> None:
    event = handler(tmp_path, FakeRuns(()), FakeSender()).handle(tick(AFTER_LAG))
    assert event is not None
    assert event.outcome == "failed"
    assert (
        event.detail == "missed night slot=2026-11-22: no nightly run for the slot yet"
    )


def test_sender_not_collected_or_missed_a_collect_still_failing_after_the_lag_is_a_missed_night(
    tmp_path: Path,
) -> None:
    sender = FakeSender(collect_status=EnumCollectStatus.READ_FAILED)
    event = handler(tmp_path, FakeRuns((run_view(),)), sender).handle(tick(AFTER_LAG))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail.startswith(
        "missed night slot=2026-11-22: collect_nightly answered read_failed"
    )
    assert [name for name, _ in sender.sent] == ["collect_nightly"]


def test_sender_not_collected_or_missed_an_unreadable_run_list_is_a_failed_run_naming_the_date(
    tmp_path: Path,
) -> None:
    event = handler(tmp_path, FakeRuns(fail=True), FakeSender()).handle(
        tick(AFTER_NIGHTLY)
    )
    assert event is not None
    assert event.outcome == "failed"
    assert "slot=2026-11-22" in event.detail
    assert "gateway refused" in event.detail


def test_sender_not_collected_or_missed_an_unwritable_state_directory_sends_nothing(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "state"
    blocker.write_text("a file where the directory should be", encoding="utf-8")
    sender = FakeSender()
    h = handler(tmp_path, FakeRuns((run_view(),)), sender)
    event = h.handle(tick(AFTER_NIGHTLY))
    assert event is not None
    assert event.outcome == "failed"
    assert "slot=2026-11-22 not sent" in event.detail
    assert sender.sent == []


def test_sender_not_collected_or_missed_an_unset_state_directory_variable_sends_nothing(
    tmp_path: Path,
) -> None:
    sender = FakeSender()
    h = handler(tmp_path, FakeRuns((run_view(),)), sender, state=False)
    event = h.handle(tick(AFTER_NIGHTLY))
    assert event is not None
    assert event.outcome == "failed"
    assert "TEST_SENDER_STATE_DIR names no writable state directory" in event.detail
    assert sender.sent == []
