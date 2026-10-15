# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A crash at each outbox boundary loses no alarm and costs at most one duplicate (OMN-20806)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.outbox_store import (
    OutboxStore,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.models.model_watchdog_delivery import (
    ModelOutboxEntry,
)
from tests.nodes.node_automation_liveness_watchdog_effect.support import (
    T0,
    FakeChannel,
    SimulatedCrash,
    high_alarm,
    make_handler,
    tick,
)

pytestmark = pytest.mark.unit


class CrashingStore(OutboxStore):
    """Dies at one named boundary of the store."""

    def __init__(self, path: Path, *, crash_after: str | None = None) -> None:
        super().__init__(path)
        self.crash_after = crash_after

    def commit_decision(
        self,
        entries: Sequence[ModelOutboxEntry],
        carried_state: dict[str, object] | None,
    ) -> None:
        super().commit_decision(entries, carried_state)
        if self.crash_after == "decision":
            self.crash_after = None
            raise SimulatedCrash("died after deciding, before sending")

    def record_delivery(
        self, entry: ModelOutboxEntry, message_ref: str, now: datetime
    ) -> ModelOutboxEntry:
        updated = super().record_delivery(entry, message_ref, now)
        if self.crash_after == "recorded":
            self.crash_after = None
            raise SimulatedCrash("died after the outbox recorded the delivery")
        return updated


def test_watchdog_outbox_crash_after_deciding_sends_on_restart(tmp_path: Path) -> None:
    channel = FakeChannel()
    alarm = high_alarm()
    store = CrashingStore(tmp_path / "w.sqlite", crash_after="decision")
    with pytest.raises(SimulatedCrash):
        tick(make_handler(tmp_path, channel, store=store), T0, alarm)
    assert channel.messages == []

    restarted = make_handler(
        tmp_path, channel, store=OutboxStore(tmp_path / "w.sqlite")
    )
    result = tick(restarted, T0 + timedelta(minutes=1))
    assert channel.count_for(alarm.episode_id) == 1
    assert [d.delivered for d in result.delivered] == [True]


def test_watchdog_outbox_crash_after_slack_accepted_finds_it_in_history(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    alarm = high_alarm()
    channel.crash_after_accept = True
    with pytest.raises(SimulatedCrash):
        tick(make_handler(tmp_path, channel), T0, alarm)
    assert channel.count_for(alarm.episode_id) == 1

    restarted = make_handler(
        tmp_path, channel, store=OutboxStore(tmp_path / "watchdog.sqlite")
    )
    result = tick(restarted, T0 + timedelta(minutes=1))
    assert channel.count_for(alarm.episode_id) == 1, (
        "history read must prevent a re-send"
    )
    assert result.reconciled == (alarm.episode_id,)
    assert [d.delivered for d in result.delivered] == [True]
    assert result.pending == 0


def test_watchdog_outbox_crash_after_slack_accepted_costs_one_duplicate_when_history_is_dark(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    alarm = high_alarm()
    channel.crash_after_accept = True
    with pytest.raises(SimulatedCrash):
        tick(make_handler(tmp_path, channel), T0, alarm)

    channel.history_unreadable = True
    restarted = make_handler(
        tmp_path, channel, store=OutboxStore(tmp_path / "watchdog.sqlite")
    )
    result = tick(restarted, T0 + timedelta(minutes=1))
    assert channel.count_for(alarm.episode_id) == 2, (
        "at least once: one duplicate at most"
    )
    assert [d.delivered for d in result.delivered] == [True]
    again = tick(restarted, T0 + timedelta(minutes=2))
    assert channel.count_for(alarm.episode_id) == 2
    assert again.delivered == ()


def test_watchdog_outbox_crash_after_outbox_recorded_sends_nothing_more(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    alarm = high_alarm()
    store = CrashingStore(tmp_path / "w.sqlite", crash_after="recorded")
    with pytest.raises(SimulatedCrash):
        tick(make_handler(tmp_path, channel, store=store), T0, alarm)
    assert channel.count_for(alarm.episode_id) == 1

    restarted = make_handler(
        tmp_path, channel, store=OutboxStore(tmp_path / "w.sqlite")
    )
    result = tick(restarted, T0 + timedelta(minutes=1))
    assert channel.count_for(alarm.episode_id) == 1
    assert result.delivered == ()
    assert result.pending == 0


def test_watchdog_outbox_redecided_alarm_is_not_queued_twice(tmp_path: Path) -> None:
    channel = FakeChannel()
    alarm = high_alarm()
    handler = make_handler(tmp_path, channel)
    tick(handler, T0, alarm)
    tick(handler, T0 + timedelta(minutes=1), alarm)
    assert channel.count_for(alarm.episode_id) == 1
