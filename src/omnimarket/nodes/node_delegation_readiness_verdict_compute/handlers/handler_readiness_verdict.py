# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shippable verdict over the delegation readiness scenarios (OMN-23181, readiness plan 2.4 'Shippable').

Pure: no I/O, no clock (``as_of`` is in the request). For every declared scenario the verdict needs all of:

* ``green_days`` (counted by ``HandlerScenarioGreenDays``) at or above ``required_green_days``;
* no slot of today (UTC) whose latest scheduled result failed;
* a successful scheduled result in the latest DUE slot, the slot that contains ``as_of`` minus the cadence's grace.
  A due slot with no scheduled run is red; a manual run never fills a slot.

A history that was not read to its end makes every scenario red (``unreadable_is_red``), and a scenario with no result
row at all is red (``no_rows``). The suite is PASS only when no scenario has a miss.
"""

from __future__ import annotations

from datetime import UTC, timedelta

from omnimarket.models.model_delegation_readiness_verdict import (
    EnumReadinessVerdict,
    ModelReadinessScenarioRow,
    ModelReadinessVerdict,
    ModelReadinessVerdictRequest,
)
from omnimarket.models.model_delegation_scenario import (
    ModelScenarioDeclaration,
    floor_to_slot,
)
from omnimarket.nodes.node_delegation_scenario_days_compute.handlers.handler_scenario_green_days import (
    HandlerScenarioGreenDays,
    _latest_per_slot,
    _settled_state,
)

UNREADABLE_IS_RED = "unreadable_is_red"


def _unreadable_row(declaration: ModelScenarioDeclaration) -> ModelReadinessScenarioRow:
    return ModelReadinessScenarioRow(
        scenario_id=declaration.scenario_id,
        green_days=0,
        failed_today=False,
        latest_due_slot=None,
        latest_due_slot_ok=False,
        misses=(UNREADABLE_IS_RED,),
        red=True,
    )


class HandlerReadinessVerdict:
    """Handler for ``node_delegation_readiness_verdict_compute``."""

    def __init__(self, green_days: HandlerScenarioGreenDays | None = None) -> None:
        self._green_days = green_days or HandlerScenarioGreenDays()

    def handle(self, request: ModelReadinessVerdictRequest) -> ModelReadinessVerdict:
        history = request.history
        as_of = history.as_of.astimezone(UTC)
        stale_after = timedelta(hours=history.stale_after_hours)
        declarations = sorted(history.scenarios, key=lambda s: s.scenario_id)
        rows: list[ModelReadinessScenarioRow] = []
        if not history.read_complete:
            rows = [_unreadable_row(declaration) for declaration in declarations]
        else:
            counted = self._green_days.handle(history).by_id()
            for declaration in declarations:
                slots = _latest_per_slot(declaration, history.results)
                failed_today = any(
                    slot.date() == as_of.date()
                    and _settled_state(result, as_of, stale_after) == "failed"
                    for slot, result in slots.items()
                )
                due_slot = floor_to_slot(
                    as_of - timedelta(hours=request.grace_hours(declaration.cadence)),
                    declaration.cadence,
                )
                due_result = slots.get(due_slot)
                due_ok = (
                    due_result is not None
                    and _settled_state(due_result, as_of, stale_after) == "green"
                )
                green_days = counted[declaration.scenario_id].green_days
                misses: list[str] = []
                if not any(
                    r.scenario_id == declaration.scenario_id for r in history.results
                ):
                    misses.append("no_rows")
                if green_days < request.required_green_days:
                    misses.append(
                        f"green_days {green_days} of {request.required_green_days}"
                    )
                if failed_today:
                    misses.append("failed_today")
                if not due_ok:
                    stamp = due_slot.strftime("%Y-%m-%dT%H:%MZ")
                    misses.append(
                        f"due_slot {stamp} "
                        + (
                            "has no scheduled run"
                            if due_result is None
                            else "not green"
                        )
                    )
                rows.append(
                    ModelReadinessScenarioRow(
                        scenario_id=declaration.scenario_id,
                        green_days=green_days,
                        failed_today=failed_today,
                        latest_due_slot=due_slot,
                        latest_due_slot_ok=due_ok,
                        misses=tuple(misses),
                        red=bool(misses),
                    )
                )
        misses_all = tuple(
            f"{row.scenario_id}:{miss}" for row in rows for miss in row.misses
        )
        if not rows:
            misses_all = ("no_scenarios_declared",)
        return ModelReadinessVerdict(
            as_of=as_of,
            required_green_days=request.required_green_days,
            window_days=history.window_days,
            verdict=EnumReadinessVerdict.FAIL
            if misses_all
            else EnumReadinessVerdict.PASS,
            rows=tuple(rows),
            misses=misses_all,
        )
