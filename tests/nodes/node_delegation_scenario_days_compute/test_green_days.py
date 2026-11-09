# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Green days of a delegation scenario from recorded histories (OMN-23171, AC1 and AC2).

Each test builds a recorded history and reads the rows the handler returns; the named breaks (a missing slot, a
cancelled slot, a manual run in a slot, a read that stops early) are the failing controls of the same reading.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.model_delegation_scenario import (
    EnumScenarioCadence,
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    ModelScenarioDeclaration,
    ModelScenarioHistory,
    ModelScenarioResult,
)
from omnimarket.nodes.node_delegation_scenario_days_compute.handlers.handler_scenario_green_days import (
    HandlerScenarioGreenDays,
)

pytestmark = pytest.mark.unit

AS_OF = datetime(2026, 11, 9, 12, 30, tzinfo=UTC)
HOURLY = ModelScenarioDeclaration(
    scenario_id="R2", cadence=EnumScenarioCadence.HOURLY, lane="lane-r2"
)
EVERY_6H = ModelScenarioDeclaration(
    scenario_id="R3", cadence=EnumScenarioCadence.EVERY_6H, lane="lane-r3"
)
DAILY = ModelScenarioDeclaration(
    scenario_id="R6", cadence=EnumScenarioCadence.DAILY, lane="lane-r6"
)


def _result(
    declaration: ModelScenarioDeclaration,
    slot: datetime,
    *,
    conclusion: EnumScenarioConclusion = EnumScenarioConclusion.SUCCESS,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
    minutes_after: int = 5,
) -> ModelScenarioResult:
    return ModelScenarioResult(
        scenario_id=declaration.scenario_id,
        slot_start=slot,
        trigger=trigger,
        conclusion=conclusion,
        recorded_at=slot + timedelta(minutes=minutes_after),
    )


def _slots(
    declaration: ModelScenarioDeclaration,
    day_offset: int,
    *,
    skip: int = 0,
    **kwargs: object,
) -> list[ModelScenarioResult]:
    """Every slot of the UTC day ``day_offset`` days before today, except the last ``skip`` slots."""
    midnight = AS_OF.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=day_offset
    )
    step = declaration.cadence.slot_hours
    count = declaration.cadence.slots_per_day - skip
    return [
        _result(declaration, midnight + timedelta(hours=step * n), **kwargs)  # type: ignore[arg-type]
        for n in range(count)
    ]


def _history(
    declaration: ModelScenarioDeclaration,
    results: list[ModelScenarioResult],
    **kwargs: object,
) -> ModelScenarioHistory:
    return ModelScenarioHistory(
        as_of=AS_OF,
        scenarios=(declaration,),
        results=tuple(results),
        **kwargs,  # type: ignore[arg-type]
    )


def _green_days(history: ModelScenarioHistory, scenario_id: str = "R2") -> int:
    return HandlerScenarioGreenDays().handle(history).by_id()[scenario_id].green_days


def test_five_full_green_days_gives_five() -> None:
    results = [r for offset in range(1, 6) for r in _slots(HOURLY, offset)]
    assert _green_days(_history(HOURLY, results)) == 5


def test_a_gap_day_ends_the_streak_older_days_do_not_count() -> None:
    results = [r for offset in (1, 2, 4, 5) for r in _slots(HOURLY, offset)]
    assert _green_days(_history(HOURLY, results)) == 2


def test_a_day_with_19_of_24_hourly_slots_settled_counts_zero() -> None:
    results = _slots(HOURLY, 1, skip=5) + [
        r for offset in range(2, 6) for r in _slots(HOURLY, offset)
    ]
    history = _history(HOURLY, results)
    assert _green_days(history) == 0
    yesterday = HandlerScenarioGreenDays().handle(history).by_id()["R2"].days[0]
    assert (yesterday.settled, yesterday.expected, yesterday.green) == (19, 24, False)


def test_a_day_with_20_of_24_hourly_slots_settled_is_green() -> None:
    results = _slots(HOURLY, 1, skip=4)
    assert _green_days(_history(HOURLY, results)) == 1


def test_a_cancelled_slot_is_not_green() -> None:
    results = _slots(HOURLY, 1)
    results[7] = _result(
        HOURLY, results[7].slot_start, conclusion=EnumScenarioConclusion.CANCELLED
    )
    assert _green_days(_history(HOURLY, results)) == 0


@pytest.mark.parametrize(
    "conclusion",
    [
        EnumScenarioConclusion.FAILURE,
        EnumScenarioConclusion.TIMED_OUT,
        EnumScenarioConclusion.SKIPPED,
        EnumScenarioConclusion.STARTUP_FAILURE,
    ],
)
def test_every_completed_conclusion_but_success_is_not_green(
    conclusion: EnumScenarioConclusion,
) -> None:
    results = _slots(HOURLY, 1)
    results[0] = _result(HOURLY, results[0].slot_start, conclusion=conclusion)
    assert _green_days(_history(HOURLY, results)) == 0


def test_a_slot_filled_only_by_a_manual_run_is_unsettled() -> None:
    results = _slots(HOURLY, 1, skip=4)  # 20 scheduled slots: exactly the bar
    manual = _result(
        HOURLY,
        results[-1].slot_start + timedelta(hours=1),
        trigger=EnumScenarioTrigger.MANUAL,
    )
    assert _green_days(_history(HOURLY, [*results, manual])) == 1
    thin = _slots(
        HOURLY, 1, skip=5
    )  # 19 scheduled; a manual run in a missing slot does not make 20
    manual_in_gap = _result(
        HOURLY,
        thin[-1].slot_start + timedelta(hours=1),
        trigger=EnumScenarioTrigger.MANUAL,
    )
    assert _green_days(_history(HOURLY, [*thin, manual_in_gap])) == 0


def test_a_manual_failure_does_not_turn_a_scheduled_slot_red() -> None:
    results = _slots(HOURLY, 1)
    results.append(
        _result(
            HOURLY,
            results[3].slot_start,
            conclusion=EnumScenarioConclusion.FAILURE,
            trigger=EnumScenarioTrigger.MANUAL,
            minutes_after=30,
        )
    )
    assert _green_days(_history(HOURLY, results)) == 1


def test_the_latest_attempt_of_a_slot_decides_it() -> None:
    results = _slots(HOURLY, 1)
    slot = results[9].slot_start
    failed_then_passed = [
        _result(
            HOURLY, slot, conclusion=EnumScenarioConclusion.FAILURE, minutes_after=5
        ),
        _result(HOURLY, slot, minutes_after=40),
    ]
    results[9:10] = failed_then_passed
    assert _green_days(_history(HOURLY, results)) == 1
    passed_then_failed = [
        _result(HOURLY, slot, minutes_after=5),
        _result(
            HOURLY, slot, conclusion=EnumScenarioConclusion.FAILURE, minutes_after=40
        ),
    ]
    results[9:11] = passed_then_failed
    assert _green_days(_history(HOURLY, results)) == 0


def test_a_run_in_progress_for_over_two_hours_is_a_failure_and_a_younger_one_is_unsettled() -> (
    None
):
    results = _slots(HOURLY, 1)
    midnight_yesterday = results[0].slot_start
    results[0] = _result(
        HOURLY, midnight_yesterday, conclusion=EnumScenarioConclusion.IN_PROGRESS
    )
    assert _green_days(_history(HOURLY, results)) == 0
    # The same run read two hours after it started is still within the bound: unsettled, 23 of 24 settled.
    within_bound = ModelScenarioHistory(
        as_of=midnight_yesterday + timedelta(days=1, minutes=30),
        scenarios=(HOURLY,),
        results=tuple(results),
        stale_after_hours=2000,
    )
    assert _green_days(within_bound) == 1


def test_every_6h_needs_four_of_four_and_daily_needs_one() -> None:
    six = [r for offset in (1, 2) for r in _slots(EVERY_6H, offset)]
    assert _green_days(_history(EVERY_6H, six), "R3") == 2
    short = _slots(EVERY_6H, 1, skip=1)
    assert _green_days(_history(EVERY_6H, short), "R3") == 0
    day = [r for offset in (1, 2, 3) for r in _slots(DAILY, offset)]
    assert _green_days(_history(DAILY, day), "R6") == 3


def test_the_day_count_is_a_parameter() -> None:
    results = [r for offset in range(1, 9) for r in _slots(HOURLY, offset)]
    assert _green_days(_history(HOURLY, results, window_days=8)) == 8
    assert _green_days(_history(HOURLY, results, window_days=5)) == 5


def test_rows_are_keyed_by_scenario_id_and_a_rerun_gives_the_same_rows() -> None:
    history = ModelScenarioHistory(
        as_of=AS_OF,
        scenarios=(DAILY, HOURLY),
        results=tuple(_slots(HOURLY, 1)),
    )
    first = HandlerScenarioGreenDays().handle(history)
    second = HandlerScenarioGreenDays().handle(history)
    assert first == second
    assert [row.scenario_id for row in first.rows] == ["R2", "R6"]
    assert first.by_id()["R6"].green_days == 0


def test_unreadable_is_red() -> None:
    results = [r for offset in range(1, 6) for r in _slots(HOURLY, offset)]
    complete = _history(HOURLY, results)
    assert _green_days(complete) == 5
    stopped = _history(
        HOURLY,
        results,
        read_complete=False,
        read_error="page 3 of the ledger rows did not answer",
    )
    row = HandlerScenarioGreenDays().handle(stopped).by_id()["R2"]
    assert (row.green_days, row.readable, row.red) == (0, False, True)
    assert row.reason.startswith("unreadable: page 3")


def test_unreadable_is_red_for_every_declared_scenario_even_with_results_read() -> None:
    history = ModelScenarioHistory(
        as_of=AS_OF,
        scenarios=(HOURLY, DAILY),
        results=tuple(_slots(HOURLY, 1) + _slots(DAILY, 1)),
        read_complete=False,
    )
    rows = HandlerScenarioGreenDays().handle(history).rows
    assert all(row.red and not row.readable for row in rows)
