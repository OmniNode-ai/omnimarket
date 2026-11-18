# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An open job-exit episode past its age limit raises again (OMN-21663).

The recorded case (fixture ``job_exit_recorded_rows_broker_lag.txt``, rows of the ledger): the
``lab-alarm-broker-lag`` job exited nonzero from 2026-11-08T18:53Z on and its episode was raised once, by the
first push; every later tick of the open episode stayed silent. Forty job-exit episodes were open since
2026-10-17 with no second raise. The decision here is pure: the caller passes the limit from its overlay.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_silent_failure_alarm_compute.handlers.handler_job_exit_alarm import (
    HandlerJobExitAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.models import (
    ModelJobExitAlarmRequest,
    ModelJobExitAlarmResult,
    ModelJobExitReading,
    ModelJobExitStreak,
)

FIXTURE = Path(__file__).parent / "fixtures" / "job_exit_recorded_rows_broker_lag.txt"
LABEL = "ai.omninode.lab-alarm-broker-lag"
LIMIT_MINUTES = 240


def _recorded_start() -> datetime:
    first = FIXTURE.read_text(encoding="utf-8").splitlines()[0].split(" | ")[0]
    return datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _tick(
    now: datetime,
    runs: int,
    last_exit: int,
    previous: tuple[ModelJobExitStreak, ...],
    *,
    reraise_after_minutes: int = LIMIT_MINUTES,
    cited: tuple[str, ...] = (),
) -> ModelJobExitAlarmResult:
    return HandlerJobExitAlarm().handle(
        ModelJobExitAlarmRequest(
            now=now,
            readable=True,
            readings=(
                ModelJobExitReading(label=LABEL, runs=runs, last_exit=last_exit),
            ),
            previous=previous,
            cited_episodes=cited,
            threshold=1,
            push_after_minutes=30,
            reraise_after_minutes=reraise_after_minutes,
            cooldown_minutes=100000,
        )
    )


def _replay(
    minutes: list[int], *, reraise_after_minutes: int = LIMIT_MINUTES
) -> list[ModelJobExitAlarmResult]:
    """Tick the job every ``minutes`` offset from the recorded first row; every run exits 2."""
    start = _recorded_start()
    previous: tuple[ModelJobExitStreak, ...] = (
        ModelJobExitStreak(label=LABEL, runs=0, last_exit=0, last_run_at=start),
    )
    results: list[ModelJobExitAlarmResult] = []
    for step, offset in enumerate(minutes, start=1):
        result = _tick(
            start + timedelta(minutes=offset),
            step,
            2,
            previous,
            reraise_after_minutes=reraise_after_minutes,
        )
        previous = result.streaks
        results.append(result)
    return results


def test_job_exit_open_episode_is_raised_again_past_the_age_limit() -> None:
    results = _replay([0, 31, 100, 280, 290])
    assert [len(r.push) for r in results] == [0, 1, 0, 0, 0]
    assert [len(r.reraised) for r in results] == [0, 0, 0, 1, 0]
    again = results[3].reraised[0]
    assert again.label == LABEL
    assert again.cause == "failed-exit"
    assert again.raise_count == 2
    assert again.age_minutes == 280
    text = results[3].alert_text[0]
    assert "STILL OPEN" in text
    assert "280 minutes" in text
    assert "raised 2 times" in text
    assert results[3].streaks[0].raise_count == 2


def test_job_exit_reraise_repeats_each_limit_and_counts_up() -> None:
    results = _replay([0, 31, 280, 300, 530])
    assert [r.reraised[0].raise_count for r in results if r.reraised] == [2, 3]
    assert "raised 3 times" in results[4].alert_text[0]


def test_job_exit_reraise_limit_is_the_callers_value_not_a_constant() -> None:
    assert not any(
        r.reraised for r in _replay([0, 31, 900, 1800], reraise_after_minutes=0)
    )
    long_limit = _replay([0, 31, 900], reraise_after_minutes=2000)
    assert not any(r.reraised for r in long_limit)
    short = _replay([0, 31, 100], reraise_after_minutes=60)
    assert len(short[2].reraised) == 1


def test_job_exit_reraise_names_whether_a_claim_cites_the_episode() -> None:
    first = _replay([0, 31])[1]
    episode = first.streaks[0].episode
    assert episode is not None
    later = _tick(
        _recorded_start() + timedelta(minutes=300),
        3,
        2,
        first.streaks,
        cited=(episode,),
    )
    assert len(later.reraised) == 1
    assert "a CLAIM cites it" in later.alert_text[0]
    uncited = _tick(_recorded_start() + timedelta(minutes=300), 3, 2, first.streaks)
    assert "no CLAIM cites it" in uncited.alert_text[0]


def test_job_exit_reraise_state_from_before_this_change_still_reads() -> None:
    old = ModelJobExitStreak.model_validate(
        {
            "label": LABEL,
            "runs": 2,
            "last_exit": 2,
            "streak": 2,
            "episode": "ep-20261108T1853Z-lab-alarm-broker-lag",
            "opened_at": "2026-11-08T18:53:31Z",
            "pushed": True,
            "cause": "failed-exit",
            "last_run_at": "2026-11-08T18:53:31Z",
        }
    )
    result = _tick(_recorded_start() + timedelta(minutes=1000), 3, 2, (old,))
    assert len(result.reraised) == 1
    assert result.reraised[0].raise_count == 2
    assert result.reraised[0].age_minutes == 1000


def test_job_exit_reraise_a_closed_episode_forgets_its_count() -> None:
    results = _replay([0, 31, 300])
    start = _recorded_start()
    healed = _tick(start + timedelta(minutes=310), 4, 0, results[-1].streaks)
    assert healed.closed
    assert healed.streaks[0].raise_count == 0
    assert healed.streaks[0].raised_at is None
    assert not healed.reraised


def test_job_exit_reraise_a_missed_interval_episode_raises_again() -> None:
    start = _recorded_start()
    previous = (
        ModelJobExitStreak(
            label=LABEL, runs=5, last_exit=0, last_run_at=start - timedelta(hours=3)
        ),
    )
    reading = ModelJobExitReading(label=LABEL, runs=5, last_exit=0, interval_s=600)

    def tick(
        offset: int, state: tuple[ModelJobExitStreak, ...]
    ) -> ModelJobExitAlarmResult:
        return HandlerJobExitAlarm().handle(
            ModelJobExitAlarmRequest(
                now=start + timedelta(minutes=offset),
                readable=True,
                readings=(reading,),
                previous=state,
                push_after_minutes=30,
                reraise_after_minutes=LIMIT_MINUTES,
                cooldown_minutes=100000,
            )
        )

    opened = tick(0, previous)
    assert opened.opened
    assert opened.opened[0].cause == "missed-interval"
    pushed = tick(31, opened.streaks)
    assert len(pushed.push) == 1
    again = tick(300, pushed.streaks)
    assert len(again.reraised) == 1
    assert again.reraised[0].cause == "missed-interval"
    assert "has not run since" in again.alert_text[0]


def test_job_exit_no_false_alarm_for_a_healthy_interval() -> None:
    start = _recorded_start()
    previous: tuple[ModelJobExitStreak, ...] = ()
    for step in range(1, 60):
        result = _tick(
            start + timedelta(minutes=10 * step),
            step,
            0,
            previous,
            reraise_after_minutes=60,
        )
        previous = result.streaks
        assert result.state == "OK"
        assert not result.opened
        assert not result.push
        assert not result.reraised
        assert not result.alert_text
        assert not result.friction_cells


def test_job_exit_no_false_alarm_for_a_young_episode() -> None:
    results = _replay([0, 31, 100, 200])
    assert not any(r.reraised for r in results)


@pytest.mark.parametrize("bad", [-1])
def test_job_exit_reraise_limit_refuses_a_negative_value(bad: int) -> None:
    with pytest.raises(ValueError, match="reraise_after_minutes"):
        ModelJobExitAlarmRequest(
            now=datetime(2026, 11, 8, tzinfo=UTC),
            readable=True,
            reraise_after_minutes=bad,
        )


def test_job_exit_no_false_alarm_for_on_time_interval_runs_after_a_healed_episode() -> (
    None
):
    """A declared interval makes the missed-interval path reachable, and a healed episode must leave no residue."""
    start = _recorded_start()
    failing = _replay([0, 31, 300])
    previous = failing[-1].streaks
    assert previous[0].episode is not None
    runs = 3
    now = start + timedelta(minutes=310)
    healed = HandlerJobExitAlarm().handle(
        ModelJobExitAlarmRequest(
            now=now,
            readable=True,
            readings=(
                ModelJobExitReading(
                    label=LABEL, runs=runs + 1, last_exit=0, interval_s=600
                ),
            ),
            previous=previous,
            push_after_minutes=30,
            reraise_after_minutes=60,
            cooldown_minutes=100000,
        )
    )
    assert healed.closed
    previous = healed.streaks
    runs += 1
    for step in range(1, 60):
        runs += 1
        result = HandlerJobExitAlarm().handle(
            ModelJobExitAlarmRequest(
                now=now + timedelta(minutes=10 * step),
                readable=True,
                readings=(
                    ModelJobExitReading(
                        label=LABEL, runs=runs, last_exit=0, interval_s=600
                    ),
                ),
                previous=previous,
                push_after_minutes=30,
                reraise_after_minutes=60,
                cooldown_minutes=100000,
            )
        )
        previous = result.streaks
        assert result.state == "OK"
        assert not result.opened
        assert not result.push
        assert not result.reraised
        assert not result.alert_text
        assert previous[0].raised_at is None
        assert previous[0].raise_count == 0


def test_job_exit_reraise_waits_for_the_push_when_the_limit_is_below_the_push_delay() -> (
    None
):
    """A limit under the push delay must not mark an episode pushed with no push sent."""
    results = _replay([0, 15, 20, 31], reraise_after_minutes=10)
    assert [len(r.reraised) for r in results] == [0, 0, 0, 0]
    assert [len(r.push) for r in results] == [0, 0, 0, 1]
    assert results[1].streaks[0].pushed is False
    assert results[3].streaks[0].pushed is True


def test_job_exit_reraise_raises_at_most_one_cap_of_old_episodes_per_tick() -> None:
    start = _recorded_start()
    labels = [f"ai.omninode.legacy-{n}" for n in range(8)]
    previous = tuple(
        ModelJobExitStreak.model_validate(
            {
                "label": label,
                "runs": 2,
                "last_exit": 2,
                "streak": 2,
                "episode": f"ep-20261108T1853Z-legacy-{n}",
                "opened_at": "2026-11-08T18:53:31Z",
                "pushed": True,
                "cause": "failed-exit",
                "last_run_at": "2026-11-08T18:53:31Z",
            }
        )
        for n, label in enumerate(labels)
    )

    def tick(
        offset: int, state: tuple[ModelJobExitStreak, ...]
    ) -> ModelJobExitAlarmResult:
        return HandlerJobExitAlarm().handle(
            ModelJobExitAlarmRequest(
                now=start + timedelta(minutes=offset),
                readable=True,
                readings=tuple(
                    ModelJobExitReading(label=label, runs=3, last_exit=2)
                    for label in labels
                ),
                previous=state,
                push_after_minutes=30,
                reraise_after_minutes=LIMIT_MINUTES,
                cooldown_minutes=100000,
                max_opened=3,
            )
        )

    first = tick(1000, previous)
    assert len(first.reraised) == 3
    second = tick(1001, first.streaks)
    assert len(second.reraised) == 3
    third = tick(1002, second.streaks)
    assert len(third.reraised) == 2
    assert (
        len({r.label for r in (*first.reraised, *second.reraised, *third.reraised)})
        == 8
    )


def test_job_exit_reraise_never_invents_a_cause_for_an_episode_that_has_none() -> None:
    old = ModelJobExitStreak.model_validate(
        {
            "label": LABEL,
            "runs": 2,
            "last_exit": 2,
            "streak": 2,
            "episode": "ep-20261108T1853Z-lab-alarm-broker-lag",
            "opened_at": "2026-11-08T18:53:31Z",
            "pushed": True,
            "last_run_at": "2026-11-08T18:53:31Z",
        }
    )
    result = _tick(_recorded_start() + timedelta(minutes=1000), 3, 2, (old,))
    assert not result.reraised
    assert not any("STILL OPEN" in text for text in result.alert_text)
