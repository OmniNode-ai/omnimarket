# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: a planted self-test contract that evaluates wrong voids the evaluation
and raises at once (OMN-20806)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAlarmSeverity,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationRunOutcome,
    EnumWatchdogSelftest,
    ModelAutomationLivenessEntry,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.handlers.handler_automation_liveness_watchdog import (
    HandlerAutomationLivenessWatchdog,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.handlers.watchdog_selftest import (
    default_planted_cases,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.handlers.watchdog_verdicts import (
    RawVerdict,
    evaluate_process,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
    ModelProcessRunFacts,
)
from tests.nodes.node_automation_liveness_watchdog_compute.support import (
    T0,
    WATCHDOG,
    Driver,
    entry,
    facts,
    overlay,
    run,
)

pytestmark = pytest.mark.unit

_V = EnumAutomationLivenessVerdict
_R = EnumAutomationLivenessReason


def test_watchdog_selftest_void_builtin_planted_contracts_pass() -> None:
    job = entry("host-a/job")
    result = Driver(overlay(job)).tick(
        T0, processes=(facts(job, [run("r1", T0 - timedelta(minutes=1))]),)
    )
    assert result.selftest is EnumWatchdogSelftest.PASS
    assert not result.void
    assert {p.expected_verdict for p in result.planted} == {
        _V.MISSED,
        _V.FAILED,
        _V.IDLE_WITH_DEMAND,
        _V.UNOBSERVABLE,
        _V.HEALTHY,
    }
    assert all(p.passed for p in result.planted)


def _blind_to_idle(
    e: ModelAutomationLivenessEntry,
    f: ModelProcessRunFacts | None,
    now: Any,
    expectations: Any = (),
    *,
    external_deadman: bool = False,
) -> RawVerdict:
    """An evaluator with a defect: it never sees IDLE_WITH_DEMAND."""
    raw = evaluate_process(e, f, now, expectations, external_deadman=external_deadman)
    if raw.verdict is _V.IDLE_WITH_DEMAND:
        return RawVerdict(_V.HEALTHY, _R.INSIDE_BOUNDS)
    return raw


def test_watchdog_selftest_void_defective_evaluator_voids_and_raises() -> None:
    job = entry("host-a/job")
    failing = facts(job, [run("r1", T0, EnumAutomationRunOutcome.FAILED)])
    driver = Driver(
        overlay(job), handler=HandlerAutomationLivenessWatchdog(_blind_to_idle)
    )
    before = driver.state

    result = driver.tick(T0 + timedelta(seconds=30), processes=(failing,))

    assert result.void
    assert result.selftest is EnumWatchdogSelftest.FAIL
    assert [p.name for p in result.planted if not p.passed] == ["idle-with-demand"]
    # Nothing about the real process left the void evaluation...
    assert [v.process_id for v in result.verdicts] == [WATCHDOG]
    assert all(r.process_id == WATCHDOG for r in result.alarms_raised)
    assert before.get(job.process_id, job.host) is None
    assert result.state.get(job.process_id, job.host) is None
    # ...and the self-test alarm went out at once, high, on the first evaluation.
    [alarm] = result.alarms_raised
    assert (alarm.verdict, alarm.reason) == (
        _V.WATCHDOG_SELFTEST_FAILED,
        _R.PLANTED_VERDICT_MISMATCH,
    )
    assert alarm.severity is EnumAlarmSeverity.HIGH
    assert alarm.delivery_due_at == alarm.opened_at + timedelta(seconds=120)
    [event] = result.verdict_events
    assert event.verdict is _V.WATCHDOG_SELFTEST_FAILED


def test_watchdog_selftest_void_wrong_planted_answer_keeps_prior_state() -> None:
    job = entry("host-a/job")
    driver = Driver(overlay(job))
    healthy = facts(job, [run("r1", T0 - timedelta(minutes=1))])
    driver.tick(T0, processes=(healthy,))
    carried = driver.state.get(job.process_id, job.host)
    assert carried is not None

    planted = list(default_planted_cases(T0 + timedelta(minutes=1)))
    planted[0] = planted[0].model_copy(update={"expected_verdict": _V.HEALTHY})
    failing = facts(
        job, [run("r2", T0 + timedelta(seconds=50), EnumAutomationRunOutcome.FAILED)]
    )
    result = driver.tick(
        T0 + timedelta(minutes=1), processes=(failing,), planted=tuple(planted)
    )

    assert result.void
    # The failed run did not open an episode while the watchdog was void.
    assert result.state.get(job.process_id, job.host) == carried
    assert [a.verdict for a in result.alarms_raised] == [_V.WATCHDOG_SELFTEST_FAILED]

    # The next sound evaluation judges the failure and starts closing the
    # self-test episode through the hysteresis.
    recovered = driver.tick(T0 + timedelta(minutes=2), processes=(failing,))
    assert not recovered.void
    assert [a.verdict for a in recovered.alarms_raised] == [_V.FAILED]
    assert recovered.alarms_cleared == ()
    driver.tick(T0 + timedelta(minutes=3), processes=(failing,))
    assert [c.verdict for c in driver.cleared] == [_V.WATCHDOG_SELFTEST_FAILED]
