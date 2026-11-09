# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the pure readiness verdict rule (OMN-23181): no I/O, no clock, the time is in the request."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.models.model_delegation_readiness_verdict import (
    EnumReadinessVerdict,
    ModelCadenceGrace,
    ModelReadinessVerdictRequest,
)
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioCadence,
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    ModelScenarioDeclaration,
    ModelScenarioHistory,
    ModelScenarioResult,
)
from omnimarket.nodes.node_delegation_readiness_verdict_compute.handlers.handler_readiness_verdict import (
    HandlerReadinessVerdict,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 12, 35, tzinfo=UTC)
MIDNIGHT = NOW.replace(hour=0, minute=0)
GRACE = (
    ModelCadenceGrace(cadence=EnumScenarioCadence.HOURLY, hours=2),
    ModelCadenceGrace(cadence=EnumScenarioCadence.EVERY_6H, hours=8),
    ModelCadenceGrace(cadence=EnumScenarioCadence.DAILY, hours=26),
)
SCENARIOS = (
    ModelScenarioDeclaration(
        scenario_id="R1", cadence=EnumScenarioCadence.HOURLY, lane="lane_r1"
    ),
    ModelScenarioDeclaration(
        scenario_id="R3", cadence=EnumScenarioCadence.EVERY_6H, lane="lane_r3"
    ),
    ModelScenarioDeclaration(
        scenario_id="R6", cadence=EnumScenarioCadence.DAILY, lane="lane_r6"
    ),
)


def _result(
    scenario_id: str,
    slot: datetime,
    conclusion: str = "success",
    trigger: str = "schedule",
    recorded_at: datetime | None = None,
) -> ModelScenarioResult:
    return ModelScenarioResult(
        scenario_id=scenario_id,
        slot_start=slot,
        trigger=EnumScenarioTrigger(trigger),
        conclusion=EnumScenarioConclusion(conclusion),
        recorded_at=recorded_at or slot + timedelta(minutes=5),
    )


def _green(
    scenario: ModelScenarioDeclaration, days: int = 7, until: datetime = NOW
) -> list[ModelScenarioResult]:
    results: list[ModelScenarioResult] = []
    slot = MIDNIGHT - timedelta(days=days)
    while slot + timedelta(minutes=5) <= until:
        results.append(_result(scenario.scenario_id, slot))
        slot += timedelta(hours=scenario.cadence.slot_hours)
    return results


def _request(
    results: list[ModelScenarioResult],
    *,
    required: int = 7,
    read_complete: bool = True,
    grace: tuple[ModelCadenceGrace, ...] = GRACE,
) -> ModelReadinessVerdictRequest:
    return ModelReadinessVerdictRequest(
        history=ModelScenarioHistory(
            as_of=NOW,
            scenarios=SCENARIOS,
            results=tuple(results),
            window_days=8,
            read_complete=read_complete,
            read_error=None if read_complete else "page 2 did not answer",
        ),
        required_green_days=required,
        grace=grace,
    )


def _everything_green() -> list[ModelScenarioResult]:
    return [r for s in SCENARIOS for r in _green(s)]


def test_golden_chain_all_seven_green_days_is_pass_with_the_due_slot_of_each_cadence() -> (
    None
):
    verdict = HandlerReadinessVerdict().handle(_request(_everything_green()))
    assert verdict.verdict is EnumReadinessVerdict.PASS
    assert verdict.passed
    assert verdict.misses == ()
    due = {row.scenario_id: row.latest_due_slot for row in verdict.rows}
    assert due == {
        "R1": MIDNIGHT + timedelta(hours=10),
        "R3": MIDNIGHT,
        "R6": MIDNIGHT - timedelta(days=1),
    }
    assert all(row.green_days >= 7 and row.latest_due_slot_ok for row in verdict.rows)


def test_golden_chain_the_verdict_is_a_pure_function_of_the_request() -> None:
    request = _request(_everything_green())
    handler = HandlerReadinessVerdict()
    assert handler.handle(request) == handler.handle(request)


def test_error_chain_one_short_names_the_scenario_and_its_count() -> None:
    results = [r for s in SCENARIOS[:1] + SCENARIOS[2:] for r in _green(s)]
    results += _green(SCENARIOS[1], days=6)
    verdict = HandlerReadinessVerdict().handle(_request(results))
    assert verdict.verdict is EnumReadinessVerdict.FAIL
    assert verdict.misses == ("R3:green_days 6 of 7",)


def test_error_chain_the_bar_comes_from_the_request() -> None:
    results = [r for s in SCENARIOS[:1] + SCENARIOS[2:] for r in _green(s)]
    results += _green(SCENARIOS[1], days=6)
    verdict = HandlerReadinessVerdict().handle(_request(results, required=6))
    assert verdict.verdict is EnumReadinessVerdict.PASS


def test_error_chain_failed_today_in_any_slot_of_today_is_red() -> None:
    results = _everything_green()
    results.append(
        _result("R3", MIDNIGHT + timedelta(hours=6), "timed_out").model_copy(
            update={"recorded_at": MIDNIGHT + timedelta(hours=6, minutes=30)}
        )
    )
    verdict = HandlerReadinessVerdict().handle(_request(results))
    assert verdict.by_id()["R3"].failed_today
    assert verdict.misses == ("R3:failed_today",)


def test_error_chain_a_failure_yesterday_breaks_the_days_not_today() -> None:
    results = _everything_green()
    results.append(
        _result("R1", MIDNIGHT - timedelta(hours=3), "failure").model_copy(
            update={"recorded_at": MIDNIGHT - timedelta(hours=2)}
        )
    )
    verdict = HandlerReadinessVerdict().handle(_request(results))
    row = verdict.by_id()["R1"]
    assert not row.failed_today
    assert (
        row.green_days == 0
    )  # yesterday had a failed slot: no consecutive green day ends there
    assert verdict.misses == ("R1:green_days 0 of 7",)


def test_error_chain_stopped_after_qualifying_each_cadence_goes_red_past_its_grace() -> (
    None
):
    """R1 stops after 08:00 (due 10:00) and R3 after yesterday's last slot (due 00:00); both keep seven green days."""
    results = (
        _green(SCENARIOS[0], until=MIDNIGHT + timedelta(hours=8, minutes=30))
        + _green(SCENARIOS[1], until=MIDNIGHT - timedelta(minutes=1))
        + _green(SCENARIOS[2])
    )
    verdict = HandlerReadinessVerdict().handle(_request(results))
    rows = verdict.by_id()
    assert [rows[i].latest_due_slot_ok for i in ("R1", "R3", "R6")] == [
        False,
        False,
        True,
    ]
    assert all(rows[i].green_days >= 7 for i in ("R1", "R3"))
    assert [miss.split(":")[0] for miss in verdict.misses] == ["R1", "R3"]


def test_error_chain_the_due_slot_follows_the_grace_of_the_request() -> None:
    results = _green(SCENARIOS[0], until=MIDNIGHT + timedelta(hours=8, minutes=30))
    results += [r for s in SCENARIOS[1:] for r in _green(s)]
    tight = HandlerReadinessVerdict().handle(_request(results))
    assert tight.by_id()["R1"].red
    wide = tuple(ModelCadenceGrace(cadence=g.cadence, hours=g.hours + 4) for g in GRACE)
    loose = HandlerReadinessVerdict().handle(_request(results, grace=wide))
    assert loose.by_id()["R1"].latest_due_slot == MIDNIGHT + timedelta(hours=6)
    assert loose.verdict is EnumReadinessVerdict.PASS


def test_error_chain_a_due_slot_still_in_progress_past_the_stale_bound_is_red() -> None:
    due = MIDNIGHT + timedelta(hours=10)
    results = [
        r
        for r in _everything_green()
        if not (r.scenario_id == "R1" and r.slot_start == due)
    ]
    results.append(_result("R1", due, "in_progress"))
    verdict = HandlerReadinessVerdict().handle(_request(results))
    assert verdict.by_id()["R1"].latest_due_slot_ok is False


def test_error_chain_a_manual_run_never_fills_a_due_slot() -> None:
    due = MIDNIGHT
    results = [
        r
        for r in _everything_green()
        if not (r.scenario_id == "R3" and r.slot_start == due)
    ]
    results.append(_result("R3", due, trigger="manual"))
    verdict = HandlerReadinessVerdict().handle(_request(results))
    assert verdict.by_id()["R3"].latest_due_slot_ok is False


def test_error_chain_unreadable_is_red_for_every_scenario() -> None:
    verdict = HandlerReadinessVerdict().handle(
        _request(_everything_green(), read_complete=False)
    )
    assert verdict.verdict is EnumReadinessVerdict.FAIL
    assert [row.misses for row in verdict.rows] == [("unreadable_is_red",)] * 3
    assert all(row.red and row.green_days == 0 for row in verdict.rows)


def test_error_chain_a_scenario_with_zero_rows_is_red() -> None:
    results = [r for r in _everything_green() if r.scenario_id != "R6"]
    verdict = HandlerReadinessVerdict().handle(_request(results))
    assert verdict.by_id()["R6"].misses[0] == "no_rows"
    assert verdict.verdict is EnumReadinessVerdict.FAIL


def test_error_chain_no_declared_scenario_is_never_a_pass() -> None:
    request = ModelReadinessVerdictRequest(
        history=ModelScenarioHistory(as_of=NOW, scenarios=(), window_days=8),
        required_green_days=7,
        grace=GRACE,
    )
    verdict = HandlerReadinessVerdict().handle(request)
    assert verdict.verdict is EnumReadinessVerdict.FAIL
    assert verdict.misses == ("no_scenarios_declared",)


def test_error_chain_a_bar_above_the_window_is_refused() -> None:
    with pytest.raises(ValidationError, match="could never be reached"):
        _request([], required=9)


def test_error_chain_a_cadence_without_grace_is_refused() -> None:
    with pytest.raises(ValidationError, match="no due-slot grace"):
        _request([], grace=GRACE[:2])
