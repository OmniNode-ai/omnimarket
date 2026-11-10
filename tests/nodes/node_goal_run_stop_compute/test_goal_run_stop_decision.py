# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21974: the stop decision of a goal-driven run, one test per reason and per rule.

AC1: all ten reasons are reachable and typed. AC2: the caps come from the experiment's data file
and the file's hash is in the run key. AC3: a cap-stopped run is right-censored, NO_PROGRESS and
AGENT_BLOCKED are failures with a censored reading beside them, OPERATOR_STOP and REFUSED_PREMISE
are excluded with their reason.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from omnimarket.models.model_delegated_code_edit import EnumCodeEditStatus
from omnimarket.models.model_goal_run import (
    EnumGoalAnalysisRole,
    EnumGoalRunAction,
    EnumGoalStopReason,
)
from omnimarket.nodes.node_goal_run_stop_compute.handlers.handler_goal_run_stop import (
    HandlerGoalRunStop,
    load_rules,
)
from omnimarket.nodes.node_goal_run_stop_compute.models.model_goal_run_stop import (
    ModelGoalRunStopDecision,
    ModelGoalRunStopRequest,
)

pytestmark = pytest.mark.unit

CAPS_FILE = """\
token_cap: 300000
wall_clock_cap_s: 7200
turn_cap: 40
compaction_cap: 3
no_progress_window_turns: 8
"""
PREDICATE_SHA = "a" * 64

ALL_TEN = {
    "GOAL_MET",
    "TOKEN_CAP",
    "WALL_CLOCK_CAP",
    "TURN_CAP",
    "COMPACTION_CAP",
    "NO_PROGRESS",
    "AGENT_BLOCKED",
    "OPERATOR_STOP",
    "INFRA_FAULT",
    "REFUSED_PREMISE",
}


def _request(
    *,
    caps_file_text: str = CAPS_FILE,
    counters: dict[str, Any] | None = None,
    signals: dict[str, Any] | None = None,
    **extra: Any,
) -> ModelGoalRunStopRequest:
    return ModelGoalRunStopRequest.model_validate(
        {
            "correlation_id": "cm13-test",
            "item_id": "item-1",
            "goal": {
                "predicate_kind": "verify_run",
                "predicate_ref": "uv run pytest tests/x -q",
                "predicate_sha256": PREDICATE_SHA,
            },
            "caps_file_text": caps_file_text,
            "counters": {
                "tokens": 1000,
                "wall_clock_s": 60,
                "turns": 2,
                "compactions": 0,
                "turns_without_subcheck_change": 0,
                **(counters or {}),
            },
            "signals": signals or {},
            **extra,
        }
    )


def _decide(**kwargs: Any) -> ModelGoalRunStopDecision:
    return HandlerGoalRunStop().handle(_request(**kwargs))


# Each case: (reason, counters, signals). One test per reason: a reason with no test fails AC1.
REACHING = [
    ("GOAL_MET", {}, {"goal_met": True}),
    ("TOKEN_CAP", {"tokens": 300000}, {}),
    ("WALL_CLOCK_CAP", {"wall_clock_s": 7200}, {}),
    ("TURN_CAP", {"turns": 40}, {}),
    ("COMPACTION_CAP", {"compactions": 3}, {}),
    ("NO_PROGRESS", {"turns_without_subcheck_change": 8}, {}),
    ("AGENT_BLOCKED", {}, {"agent_blocked": True, "detail": "cannot proceed"}),
    ("OPERATOR_STOP", {}, {"operator_stop": True}),
    ("REFUSED_PREMISE", {}, {"refused_premise": True, "detail": "premise is false"}),
]


@pytest.mark.parametrize(("reason", "counters", "signals"), REACHING)
def test_a_run_that_reaches_each_stop_returns_its_typed_reason(
    reason: str, counters: dict[str, Any], signals: dict[str, Any]
) -> None:
    decision = _decide(counters=counters, signals=signals)
    assert decision.action is EnumGoalRunAction.STOP
    assert decision.stop_reason is EnumGoalStopReason[reason]


def test_infra_fault_is_the_tenth_reason_once_its_retries_are_spent() -> None:
    limit = load_rules().infra_fault_retry_limit
    decision = _decide(
        signals={"infra_fault": True, "detail": "workspace: no space"},
        infra_fault_retries_used=limit,
    )
    assert decision.action is EnumGoalRunAction.STOP
    assert decision.stop_reason is EnumGoalStopReason.INFRA_FAULT


def test_the_enum_has_exactly_the_ten_reasons_and_each_is_in_a_test() -> None:
    assert {r.name for r in EnumGoalStopReason} == ALL_TEN
    tested = {name for name, _, _ in REACHING} | {"INFRA_FAULT"}
    assert tested == ALL_TEN


def test_the_contract_gives_every_reason_a_role() -> None:
    assert set(load_rules().stop_roles) == set(EnumGoalStopReason)


def test_a_run_below_every_cap_continues() -> None:
    decision = _decide()
    assert decision.action is EnumGoalRunAction.CONTINUE
    assert decision.stop_reason is None
    assert decision.analysis is None


def test_a_counter_one_short_of_a_cap_does_not_stop() -> None:
    decision = _decide(
        counters={
            "tokens": 299999,
            "wall_clock_s": 7199,
            "turns": 39,
            "compactions": 2,
            "turns_without_subcheck_change": 7,
        }
    )
    assert decision.action is EnumGoalRunAction.CONTINUE


# --- first reason wins --------------------------------------------------------------------------


def test_a_reason_already_recorded_wins_over_a_later_one() -> None:
    first = _decide(counters={"tokens": 300000})
    assert first.stop_reason is EnumGoalStopReason.TOKEN_CAP
    later = _decide(
        counters={"tokens": 350000, "turns": 40},
        signals={"goal_met": True},
        prior_stop=first.record(),
    )
    assert later.stop_reason is EnumGoalStopReason.TOKEN_CAP
    assert later.counters_at_stop == first.counters_at_stop
    assert later.counters_at_stop.tokens == 300000


def test_two_reasons_at_one_instant_resolve_by_the_contract_order() -> None:
    both = _decide(counters={"tokens": 300000, "turns": 40})
    assert both.stop_reason is EnumGoalStopReason.TOKEN_CAP
    assert set(both.reached_reasons) == {
        EnumGoalStopReason.TOKEN_CAP,
        EnumGoalStopReason.TURN_CAP,
    }
    met_on_the_last_turn = _decide(counters={"turns": 40}, signals={"goal_met": True})
    assert met_on_the_last_turn.stop_reason is EnumGoalStopReason.GOAL_MET


def test_the_stop_carries_the_counters_at_that_instant() -> None:
    decision = _decide(counters={"turns": 40, "tokens": 12345})
    assert decision.counters_at_stop.turns == 40
    assert decision.counters_at_stop.tokens == 12345


# --- a fault is retried, not scored -------------------------------------------------------------


def test_an_infra_fault_with_retries_left_is_retried_and_not_scored() -> None:
    decision = _decide(
        signals={"infra_fault": True, "detail": "broker timeout"},
        infra_fault_retries_used=0,
    )
    assert decision.action is EnumGoalRunAction.RETRY
    assert decision.stop_reason is None
    assert decision.analysis is None
    assert decision.retry_reason is EnumGoalStopReason.INFRA_FAULT


def test_a_retry_keeps_the_run_key() -> None:
    first = _decide(signals={"infra_fault": True}, infra_fault_retries_used=0)
    again = _decide(signals={"infra_fault": True}, infra_fault_retries_used=1)
    assert first.run_key == again.run_key


def test_a_fault_that_outlasts_its_retries_is_excluded_with_its_reason() -> None:
    limit = load_rules().infra_fault_retry_limit
    decision = _decide(
        signals={"infra_fault": True, "detail": "broker timeout"},
        infra_fault_retries_used=limit,
    )
    assert decision.analysis is not None
    assert decision.analysis.role is EnumGoalAnalysisRole.EXCLUDED
    assert decision.analysis.scored_as_giving_up is False
    assert "broker timeout" in decision.analysis.exclusion_reason
    assert decision.analysis.counted is True


# --- analysis roles (AC3) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "counters", "field", "cap"),
    [
        ("TOKEN_CAP", {"tokens": 350000}, "tokens", 300000),
        ("WALL_CLOCK_CAP", {"wall_clock_s": 7300}, "wall_clock_s", 7200),
        ("TURN_CAP", {"turns": 41}, "turns", 40),
        ("COMPACTION_CAP", {"compactions": 4}, "compactions", 3),
    ],
)
def test_a_cap_stopped_run_is_flagged_and_right_censored_at_the_cap(
    reason: str, counters: dict[str, Any], field: str, cap: int
) -> None:
    decision = _decide(counters=counters)
    assert decision.stop_reason is EnumGoalStopReason[reason]
    analysis = decision.analysis
    assert analysis is not None
    assert analysis.role is EnumGoalAnalysisRole.RIGHT_CENSORED
    assert analysis.right_censored is True
    assert analysis.counts_as_success is False
    assert analysis.in_denominator is True
    assert analysis.scored_as_giving_up is False
    assert analysis.alternate_role is None
    # censored AT the cap: the capped dimension carries the cap, the counter keeps what was seen
    assert analysis.censored_at is not None
    assert getattr(analysis.censored_at, field) == cap
    assert getattr(decision.counters_at_stop, field) == counters[field]


def test_the_goal_met_run_is_an_observed_time_and_token_count() -> None:
    decision = _decide(
        counters={"tokens": 5000, "wall_clock_s": 300}, signals={"goal_met": True}
    )
    analysis = decision.analysis
    assert analysis is not None
    assert analysis.role is EnumGoalAnalysisRole.OBSERVED
    assert analysis.counts_as_success is True
    assert analysis.right_censored is False
    assert analysis.censored_at is None
    assert decision.counters_at_stop.tokens == 5000


@pytest.mark.parametrize(
    ("reason", "counters", "signals"),
    [
        ("NO_PROGRESS", {"turns_without_subcheck_change": 8}, {}),
        ("AGENT_BLOCKED", {}, {"agent_blocked": True}),
    ],
)
def test_no_progress_and_agent_blocked_are_failures_with_a_censored_reading(
    reason: str, counters: dict[str, Any], signals: dict[str, Any]
) -> None:
    decision = _decide(counters=counters, signals=signals)
    assert decision.stop_reason is EnumGoalStopReason[reason]
    analysis = decision.analysis
    assert analysis is not None
    assert analysis.role is EnumGoalAnalysisRole.FAILURE
    assert analysis.right_censored is False
    assert analysis.counts_as_success is False
    assert analysis.in_denominator is True
    assert analysis.alternate_role is EnumGoalAnalysisRole.RIGHT_CENSORED
    assert analysis.scored_as_giving_up is True


@pytest.mark.parametrize(
    ("reason", "signals"),
    [
        ("OPERATOR_STOP", {"operator_stop": True}),
        ("REFUSED_PREMISE", {"refused_premise": True, "detail": "premise is false"}),
    ],
)
def test_operator_stop_and_refused_premise_are_excluded_with_their_reason(
    reason: str, signals: dict[str, Any]
) -> None:
    decision = _decide(signals=signals)
    assert decision.stop_reason is EnumGoalStopReason[reason]
    analysis = decision.analysis
    assert analysis is not None
    assert analysis.role is EnumGoalAnalysisRole.EXCLUDED
    assert analysis.in_denominator is False
    assert analysis.counts_as_success is False
    assert analysis.counted is True
    assert analysis.exclusion_reason


def test_only_goal_met_counts_as_a_success() -> None:
    successes = {
        name
        for name, counters, signals in REACHING
        if (a := _decide(counters=counters, signals=signals).analysis)
        and a.counts_as_success
    }
    assert successes == {"GOAL_MET"}


# --- AC2: caps from the data file; its hash is in the run key ---------------------------------------


def test_the_caps_are_read_from_the_data_file() -> None:
    decision = _decide()
    assert decision.caps.token_cap == 300000
    assert decision.caps.wall_clock_cap_s == 7200
    assert decision.caps.turn_cap == 40
    assert decision.caps.compaction_cap == 3
    assert decision.caps.no_progress_window_turns == 8


def test_the_file_hash_is_the_sha256_of_its_bytes() -> None:
    decision = _decide()
    assert decision.caps_file_sha256 == hashlib.sha256(CAPS_FILE.encode()).hexdigest()


def test_a_cap_changed_in_the_file_changes_the_run_key() -> None:
    base = _decide()
    changed = _decide(caps_file_text=CAPS_FILE.replace("turn_cap: 40", "turn_cap: 41"))
    assert changed.caps.turn_cap == 41
    assert changed.caps_file_sha256 != base.caps_file_sha256
    assert changed.run_key != base.run_key


def test_the_same_file_gives_the_same_run_key() -> None:
    assert _decide().run_key == _decide().run_key


def test_a_changed_goal_or_item_changes_the_run_key() -> None:
    base = _decide()
    other_goal = ModelGoalRunStopRequest.model_validate(
        {
            **_request().model_dump(mode="json"),
            "goal": {
                **_request().goal.model_dump(mode="json"),
                "predicate_sha256": "b" * 64,
            },
        }
    )
    other_item = _request().model_copy(update={"item_id": "item-2"})
    assert HandlerGoalRunStop().handle(other_goal).run_key != base.run_key
    assert HandlerGoalRunStop().handle(other_item).run_key != base.run_key


@pytest.mark.parametrize(
    "bad",
    [
        "token_cap: 0\nwall_clock_cap_s: 1\nturn_cap: 1\ncompaction_cap: 1\nno_progress_window_turns: 1\n",
        "token_cap: 1\nwall_clock_cap_s: 1\nturn_cap: 1\ncompaction_cap: 1\n",
        CAPS_FILE + "extra_cap: 1\n",
        "- not\n- a mapping\n",
        "",
    ],
)
def test_a_caps_file_that_is_not_the_five_positive_caps_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match=r"caps|validation error"):
        _decide(caps_file_text=bad)


# --- the code-edit loop's stop states, mapped as data -----------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (EnumCodeEditStatus.BUDGET_EXHAUSTED, EnumGoalStopReason.TURN_CAP),
        (EnumCodeEditStatus.NO_PROGRESS, EnumGoalStopReason.NO_PROGRESS),
        (EnumCodeEditStatus.ACCEPTED, EnumGoalStopReason.GOAL_MET),
    ],
)
def test_the_loops_stop_states_map_to_the_enum(
    status: EnumCodeEditStatus, expected: EnumGoalStopReason
) -> None:
    decision = _decide(loop_status={"loop": "code_edit", "status": status.value})
    assert decision.action is EnumGoalRunAction.STOP
    assert decision.stop_reason is expected


@pytest.mark.parametrize(
    "status", [EnumCodeEditStatus.INFRA_ERROR, EnumCodeEditStatus.DELEGATE_FAILED]
)
def test_the_loops_faults_map_to_infra_fault_and_are_retried(
    status: EnumCodeEditStatus,
) -> None:
    decision = _decide(loop_status={"loop": "code_edit", "status": status.value})
    assert decision.action is EnumGoalRunAction.RETRY
    assert decision.retry_reason is EnumGoalStopReason.INFRA_FAULT


def test_a_loop_state_that_is_not_a_stop_continues() -> None:
    decision = _decide(
        loop_status={
            "loop": "code_edit",
            "status": EnumCodeEditStatus.CHECKS_FAILED.value,
        }
    )
    assert decision.action is EnumGoalRunAction.CONTINUE


def test_every_state_of_the_loop_enum_is_mapped_in_the_contract() -> None:
    mapped = set(load_rules().loop_status_map["code_edit"])
    assert mapped == {s.value for s in EnumCodeEditStatus}


def test_a_loop_state_the_contract_does_not_map_is_refused() -> None:
    with pytest.raises(ValueError, match="is not mapped"):
        _decide(loop_status={"loop": "code_edit", "status": "invented"})
    with pytest.raises(ValueError, match="no stop-state map"):
        _decide(loop_status={"loop": "unknown_loop", "status": "accepted"})
