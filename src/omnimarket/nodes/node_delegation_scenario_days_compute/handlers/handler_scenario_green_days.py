# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consecutive green UTC days of each delegation readiness scenario (OMN-23171, readiness plan 2.4).

Rules, all from the plan and all pure:

* Only ``schedule`` results fill a slot; a manual run never does.
* One result per slot: the latest attempt (the newest ``recorded_at``).
* A completed conclusion other than ``success`` is not green. A run still queued or in progress after
  ``stale_after_hours`` is not green; a younger one is unsettled and counts as neither settled nor failed.
* A day is green when at least the cadence's share of slots settled and every settled slot succeeded.
* ``green_days`` is the count of consecutive green days ending yesterday, over at most ``window_days`` days.
* A history that was not read to its end is red for every scenario, whatever the results read so far say.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    ModelScenarioDay,
    ModelScenarioDeclaration,
    ModelScenarioGreenDays,
    ModelScenarioGreenDaysRow,
    ModelScenarioHistory,
    ModelScenarioResult,
    floor_to_slot,
    previous_days,
)


def _latest_per_slot(
    declaration: ModelScenarioDeclaration, results: tuple[ModelScenarioResult, ...]
) -> dict[datetime, ModelScenarioResult]:
    slots: dict[datetime, ModelScenarioResult] = {}
    for result in results:
        if (
            result.scenario_id != declaration.scenario_id
            or result.trigger is not EnumScenarioTrigger.SCHEDULE
        ):
            continue
        slot = floor_to_slot(result.slot_start, declaration.cadence)
        held = slots.get(slot)
        if held is None or result.recorded_at > held.recorded_at:
            slots[slot] = result
    return slots


def _settled_state(
    result: ModelScenarioResult, as_of: datetime, stale_after: timedelta
) -> str:
    """``green``, ``failed`` or ``unsettled`` for the latest result of one slot."""
    if not result.conclusion.unfinished:
        return (
            "green" if result.conclusion is EnumScenarioConclusion.SUCCESS else "failed"
        )
    waited = as_of - result.recorded_at
    return "failed" if waited > stale_after else "unsettled"


def _day(
    day: date,
    declaration: ModelScenarioDeclaration,
    slots: dict[datetime, ModelScenarioResult],
    as_of: datetime,
    stale_after: timedelta,
) -> ModelScenarioDay:
    settled = failed = 0
    for slot, result in slots.items():
        if slot.date() != day:
            continue
        state = _settled_state(result, as_of, stale_after)
        if state == "unsettled":
            continue
        settled += 1
        failed += state == "failed"
    cadence = declaration.cadence
    return ModelScenarioDay(
        day=day,
        expected=cadence.slots_per_day,
        settled=settled,
        failed=failed,
        green=settled >= cadence.required_settled and failed == 0,
    )


class HandlerScenarioGreenDays:
    """Handler for ``node_delegation_scenario_days_compute``."""

    def handle(self, request: ModelScenarioHistory) -> ModelScenarioGreenDays:
        as_of = request.as_of.astimezone(UTC)
        stale_after = timedelta(hours=request.stale_after_hours)
        days = previous_days(as_of, request.window_days)
        rows: list[ModelScenarioGreenDaysRow] = []
        for declaration in sorted(request.scenarios, key=lambda s: s.scenario_id):
            if not request.read_complete:
                rows.append(
                    ModelScenarioGreenDaysRow(
                        scenario_id=declaration.scenario_id,
                        cadence=declaration.cadence,
                        green_days=0,
                        readable=False,
                        red=True,
                        reason=f"unreadable: {request.read_error or 'the history was not read to its end'}",
                    )
                )
                continue
            slots = _latest_per_slot(declaration, request.results)
            readings = tuple(
                _day(day, declaration, slots, as_of, stale_after) for day in days
            )
            green_days = 0
            for reading in readings:
                if not reading.green:
                    break
                green_days += 1
            newest = readings[0]
            rows.append(
                ModelScenarioGreenDaysRow(
                    scenario_id=declaration.scenario_id,
                    cadence=declaration.cadence,
                    green_days=green_days,
                    readable=True,
                    red=green_days == 0,
                    reason=(
                        f"{green_days} consecutive green days"
                        if green_days
                        else (
                            f"{newest.day.isoformat()}: {newest.settled} of {newest.expected} slots settled "
                            f"({declaration.cadence.required_settled} needed), {newest.failed} failed"
                        )
                    ),
                    days=readings,
                )
            )
        return ModelScenarioGreenDays(
            as_of=as_of, window_days=request.window_days, rows=tuple(rows)
        )
