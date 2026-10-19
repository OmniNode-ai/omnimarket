# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily canary episode proves the alarm path (OMN-20807, AC3)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
)
from omnimarket.models.liveness.model_automation_liveness_deadman import (
    CANARY_INJECTION_HOUR_UTC,
    CANARY_PROCESS_ID,
    ModelAutomationLivenessDeadmanRequest,
)
from omnimarket.nodes.node_automation_liveness_deadman_compute.handlers.handler_automation_liveness_deadman import (
    HandlerAutomationLivenessDeadman,
)
from tests.nodes.node_automation_liveness_deadman_effect.support import (
    canary_name,
    canary_raised,
    cleared,
    delivered,
    episode_id,
    recorded_event,
)

pytestmark = pytest.mark.unit

DAYS = (date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12))
NOW = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)


def _injection(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, CANARY_INJECTION_HOUR_UTC, tzinfo=UTC)


def _facts(
    *,
    delivered_days: tuple[date, ...] = DAYS,
    recorded_days: tuple[date, ...] = DAYS,
    opened_days: tuple[date, ...] = DAYS,
    closed_days: tuple[date, ...] = DAYS,
) -> dict[str, object]:
    def name(d: date) -> str:
        return canary_name(d.isoformat())

    return {
        "opened": tuple(
            canary_raised(d.isoformat(), _injection(d) + timedelta(minutes=2))
            for d in opened_days
        ),
        "delivered": tuple(
            delivered(name(d), _injection(d) + timedelta(minutes=3))
            for d in delivered_days
        ),
        "recorded": tuple(
            recorded_event(name(d), _injection(d) + timedelta(minutes=4))
            for d in recorded_days
        ),
        "closed": tuple(
            cleared(name(d), _injection(d), _injection(d) + timedelta(hours=1))
            for d in closed_days
        ),
    }


def _evaluate(now: datetime = NOW, **facts: object):
    return HandlerAutomationLivenessDeadman().handle(
        ModelAutomationLivenessDeadmanRequest.model_validate(
            {
                "now": now,
                "primary_host": "host-p",
                "canary_host": "host-q",
                "primary_last_heartbeat_at": now - timedelta(seconds=30),
                "canary_dates": DAYS,
                **facts,
            }
        )
    )


def test_deadman_canary_daily_episodes_three_folded_days_each_open_and_close() -> None:
    result = _evaluate(**_facts())
    assert result.alarms_raised == ()
    assert [d.day for d in result.canary] == list(DAYS)
    assert all(
        d.judged and d.opened and d.delivered and d.recorded and d.closed
        for d in result.canary
    )
    assert len({d.episode_id for d in result.canary}) == 3
    assert [d.episode_id for d in result.canary] == [
        episode_id(canary_name(d.isoformat())) for d in DAYS
    ]


def test_deadman_canary_daily_episodes_a_day_missing_delivered_breaks_the_path() -> (
    None
):
    result = _evaluate(**_facts(delivered_days=(DAYS[0], DAYS[2])))
    (alarm,) = result.alarms_raised
    assert alarm.verdict is EnumAutomationLivenessVerdict.ALARM_PATH_BROKEN
    assert alarm.reason is EnumAutomationLivenessReason.CANARY_UNDELIVERED
    assert alarm.process_id == CANARY_PROCESS_ID
    assert alarm.host == "host-q"
    assert "deadman:canary-2026-10-11" in alarm.evidence_ref


def test_deadman_canary_daily_episodes_a_day_missing_recorded_breaks_the_path() -> None:
    result = _evaluate(**_facts(recorded_days=(DAYS[0], DAYS[1])))
    (alarm,) = result.alarms_raised
    assert alarm.verdict is EnumAutomationLivenessVerdict.ALARM_PATH_BROKEN
    assert alarm.reason is EnumAutomationLivenessReason.CANARY_UNRECORDED
    assert "deadman:canary-2026-10-12" in alarm.evidence_ref


def test_deadman_canary_daily_episodes_a_day_with_no_episode_is_undelivered() -> None:
    result = _evaluate(**_facts(opened_days=DAYS[:2]))
    (alarm,) = result.alarms_raised
    assert alarm.reason is EnumAutomationLivenessReason.CANARY_UNDELIVERED
    assert result.canary[2].opened is False


def test_deadman_canary_daily_episodes_a_day_is_not_judged_before_its_deadline() -> (
    None
):
    early = _injection(DAYS[2]) + timedelta(minutes=5)
    result = _evaluate(
        now=early,
        **_facts(delivered_days=(), recorded_days=(), opened_days=(), closed_days=()),
    )
    assert [d.judged for d in result.canary] == [True, True, False]
    assert [a.evidence_ref for a in result.alarms_raised] == [
        f"deadman:canary-{d.isoformat()}:opened=False:delivered=False:recorded=False"
        for d in DAYS[:2]
    ]


def test_deadman_canary_daily_episodes_another_days_delivery_does_not_count() -> None:
    """A delivery for day one must not satisfy day two."""
    result = _evaluate(**_facts(delivered_days=(DAYS[0],)))
    assert {a.reason for a in result.alarms_raised} == {
        EnumAutomationLivenessReason.CANARY_UNDELIVERED
    }
    assert len(result.alarms_raised) == 2
