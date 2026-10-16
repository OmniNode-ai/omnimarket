# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delivery by due time: a digest without retry, an overdue alarm retried then escalated (OMN-20806)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.liveness.model_automation_liveness import EnumAlarmDeliveryRoute
from tests.nodes.node_automation_liveness_watchdog_effect.support import (
    FakeChannel,
    FakeEscalation,
    high_alarm,
    low_alarm,
    make_handler,
    tick,
)

pytestmark = pytest.mark.unit

OPENED = datetime(2026, 10, 9, 1, 0, tzinfo=UTC)
DIGEST = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)


def test_watchdog_delivery_due_time_low_severity_waits_for_its_digest(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    escalation = FakeEscalation()
    handler = make_handler(tmp_path, channel, escalation)
    alarm = low_alarm(opened_at=OPENED)

    tick(handler, OPENED, alarm)
    for minutes in range(1, 300):
        result = tick(handler, OPENED + timedelta(minutes=minutes))
        assert result.delivered == (), f"posted early at +{minutes} min"
    assert channel.attempts == 0

    digest = tick(handler, DIGEST)
    assert channel.attempts == 1
    assert [d.delivered for d in digest.delivered] == [True]
    assert digest.delivered[0].attempted_at <= alarm.delivery_due_at
    assert digest.retried == ()
    assert digest.escalated == ()
    assert escalation.calls == []

    after = tick(handler, DIGEST + timedelta(minutes=5))
    assert after.delivered == ()
    assert channel.attempts == 1


def test_watchdog_delivery_due_time_high_severity_posts_at_once(tmp_path: Path) -> None:
    channel = FakeChannel()
    handler = make_handler(tmp_path, channel)
    alarm = high_alarm()
    result = tick(handler, alarm.opened_at, alarm)
    assert channel.attempts == 1
    assert result.delivered[0].delivered is True


def test_watchdog_delivery_due_time_overdue_is_retried_then_escalated(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    channel.refuse_with = "slack 503"
    order: list[str] = []
    escalation = FakeEscalation(order)
    handler = make_handler(tmp_path, channel, escalation)
    alarm = high_alarm(due_seconds=120)

    inside = tick(handler, alarm.opened_at, alarm)
    assert inside.escalated == ()
    assert channel.attempts == 1

    still_inside = tick(handler, alarm.opened_at + timedelta(seconds=60))
    assert still_inside.escalated == ()
    assert escalation.calls == []

    before = channel.attempts
    overdue = tick(handler, alarm.opened_at + timedelta(seconds=180))
    assert channel.attempts == before + 1, "retried before escalating"
    assert overdue.retried == (alarm.episode_id,)
    assert overdue.escalated == (alarm.episode_id,)
    assert escalation.calls == [alarm.episode_id]
    routes = [d.route for d in overdue.delivered]
    assert routes == [EnumAlarmDeliveryRoute.SLACK, EnumAlarmDeliveryRoute.GITHUB_ISSUE]
    assert overdue.delivered[-1].delivered is True
    assert overdue.delivered[-1].message_ref == "issue#1"

    again = tick(handler, alarm.opened_at + timedelta(seconds=240))
    assert escalation.calls == [alarm.episode_id], "escalated once, not every tick"
    assert again.escalated == ()


def test_watchdog_delivery_due_time_overdue_but_delivered_on_retry_is_not_escalated(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    channel.refuse_with = "slack 503"
    escalation = FakeEscalation()
    handler = make_handler(tmp_path, channel, escalation)
    alarm = high_alarm(due_seconds=120)
    tick(handler, alarm.opened_at, alarm)

    channel.refuse_with = None
    late = tick(handler, alarm.opened_at + timedelta(seconds=180))
    assert [d.delivered for d in late.delivered] == [True]
    assert late.escalated == ()
    assert escalation.calls == []


def test_watchdog_delivery_due_time_cap_folds_a_storm_into_one_digest(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    handler = make_handler(tmp_path, channel)
    alarms = [high_alarm(f"proc-{n:02d}") for n in range(25)]
    result = tick(handler, alarms[0].opened_at, *alarms)
    assert len(channel.messages) == 21, "20 messages and one digest naming the rest"
    assert result.digested == 5
    assert result.pending == 0
    assert "5" in channel.messages[-1][1]
    for alarm in alarms:
        assert channel.count_for(alarm.episode_id) == 1
