# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Behaviour of the lane work reducer: the repeat rule, deadlines, help budget, replay (OMN-26706)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from omnibase_core.enums.enum_lane_work_state import EnumLaneWorkState

from omnimarket.nodes.node_fsm_single_source_check_compute.handlers.handler_fsm_single_source_check import (
    HandlerFsmSingleSourceCheck,
)
from omnimarket.nodes.node_fsm_single_source_check_compute.models.model_fsm_single_source_check import (
    ModelFsmSingleSourceCheckRequest,
    ModelSourceText,
)
from omnimarket.nodes.node_lane_work_reducer.handlers.handler_lane_work_reducer import (
    HandlerLaneWorkReducer,
    deadline_event,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_fold_input import (
    ModelLaneWorkFoldInput,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reduce_request import (
    ModelLaneWorkReduceRequest,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_config import (
    ModelLaneWorkReducerConfig,
)

from .conftest_lane_work import (
    CONTRACT,
    HEAD,
    OTHER,
    T0,
    TIERS,
    Driver,
    contract_machine,
)

pytestmark = pytest.mark.unit

NODE = CONTRACT.parent
WORKER_OWNED = (
    "awaiting_worker",
    "red",
    "diagnosing",
    "fixing",
    "waiting_release",
    "pushing",
)


def to_proving(driver: Driver) -> None:
    driver.send("start", worker_tier="claude-medium")
    driver.send("worker_started", lane_name="lane-x")
    driver.send("claim_confirmed")
    driver.send("check_green", head_sha=HEAD, observed_at=T0, ci_read_at=T0)
    assert driver.name == "proving"


def to_handing_off(driver: Driver) -> None:
    to_proving(driver)
    driver.send("proof_passed", head_sha=HEAD)
    driver.send("proof_posted")
    assert driver.name == "handing_off"


# ------------------------------------------------------------------------ AC2/3


def test_ac2_handler_has_no_io_clock_uuid_or_model_call() -> None:
    text = (NODE / "handlers" / "handler_lane_work_reducer.py").read_text()
    pattern = re.compile(
        r"datetime.now|time.time|uuid4|open\(|subprocess|httpx|requests|execute_transition"
    )
    assert [line for line in text.splitlines() if pattern.search(line)] == []


def test_ac3_enum_equals_the_contract_state_names() -> None:
    declared = [state["state_name"] for state in contract_machine()["states"]]
    assert [member.value for member in EnumLaneWorkState] == declared


def test_ac3_single_source_check_passes_over_the_node() -> None:
    sources = tuple(
        ModelSourceText(name=str(path.relative_to(NODE)), text=path.read_text())
        for path in sorted(NODE.rglob("*.py"))
    )
    result = HandlerFsmSingleSourceCheck().handle(
        ModelFsmSingleSourceCheckRequest(
            contract_text=CONTRACT.read_text(), sources=sources
        )
    )
    # The check also wants a call to the core executor. The ticket forbids that call
    # in this handler (AC2), so that one issue is the known conflict; any other issue
    # (a state table, a branch chain) is a real failure.
    assert {issue.code.value for issue in result.issues} <= {
        "execute_transition_missing"
    }
    assert len(result.state_names) == 16


def test_every_help_entering_edge_has_its_paired_blocked_edge() -> None:
    transitions = contract_machine()["transitions"]
    entering = [
        t
        for t in transitions
        if t["to_state"] == "help_requested" and t["from_state"] != "help_requested"
    ]
    assert entering
    for edge in entering:
        assert any(
            c["expression"] == "help_budget_left == true" for c in edge["conditions"]
        ), edge["transition_name"]
        paired = [
            t
            for t in transitions
            if t["from_state"] == edge["from_state"]
            and t["trigger"] == edge["trigger"]
            and t["to_state"] == "blocked"
            and t["priority"] < edge["priority"]
            and t["actions"][0]["action_config"]
            == [
                ["decision", "the evidence and the question for the operator"],
                ["reason", "help_exhausted"],
            ]
        ]
        assert paired, edge["transition_name"]


# -------------------------------------------------------------------------- AC4


def test_ac4_unclassified_counts_across_states() -> None:
    d = Driver("red")
    d.send("unclassified")
    assert d.name == "diagnosing"
    d.send("cause_found")
    assert d.name == "fixing"
    d.send("unclassified")
    assert d.name == "help_requested"
    assert d.state.repeat_counters == {"unclassified:": 2}


def test_ac4_push_refused_check_failed_across_a_loop() -> None:
    d = Driver("pushing")
    d.send("push_refused", cls="check_failed")
    assert d.name == "fixing"
    d.send("fix_committed")
    assert d.name == "pushing"
    d.send("push_refused", cls="check_failed")
    assert d.name == "help_requested"


def test_ac4_refused_input_is_not_counted() -> None:
    d = Driver("diagnosing")
    result = d.send("push_refused", cls="check_failed")
    assert result.outcome == "refused"
    assert d.name == "diagnosing"
    assert d.state.repeat_counters == {}
    d.send("push_refused", cls="check_failed")
    assert d.state.repeat_counters == {}
    assert d.name == "diagnosing"


def test_ac4_pushed_never_goes_to_help() -> None:
    d = Driver("pushing")
    for _ in range(3):
        d.state = d.handler.initial_state("pushing", config=d.config).model_copy(
            update={"repeat_counters": d.state.repeat_counters}
        )
        result = d.send("pushed", head_sha=HEAD)
        assert result.outcome == "transition"
        assert d.name == "proving"
    assert d.state.repeat_counters == {}


def test_ac4_counters_reset_on_proof_passed() -> None:
    d = Driver("red")
    d.send("unclassified")
    assert d.state.repeat_counters
    d.state = d.handler.initial_state("proving", config=d.config).model_copy(
        update={"repeat_counters": d.state.repeat_counters, "expected_head": HEAD}
    )
    result = d.send("proof_passed", head_sha=HEAD)
    assert result.outcome == "transition"
    assert d.state.repeat_counters == {}


def test_ac4_release_identity_is_not_counted() -> None:
    d = Driver("fixing")
    for _ in range(3):
        d.send("commit_refused", cls="release_identity")
        assert d.name == "waiting_release"
        d.fire("poll")
        assert d.name == "fixing"
    assert d.state.repeat_counters == {}


# -------------------------------------------------------------------------- AC5


@pytest.mark.parametrize("entry", WORKER_OWNED)
def test_ac5_worker_deadline_fires_as_armed(entry: str) -> None:
    d = Driver(entry)
    assert "worker" in d.state.armed_deadlines
    result = d.fire("worker")
    assert result.outcome == "transition"
    assert d.name == "blocked"
    want = "worker_never_started" if entry == "awaiting_worker" else "worker_lost"
    terminal = result.intents[0]
    assert terminal.kind == "terminal"
    assert terminal.inputs["reason"] == want
    assert terminal.inputs["last_receipt"].endswith("-0")


def test_ac5_renewed_deadline_refuses_the_old_arming() -> None:
    d = Driver("red")
    old = d.state.armed_deadlines["worker"]
    d.send("log_read", red_kind="tests")
    assert d.name == "diagnosing"
    assert d.state.armed_deadlines["worker"] != old
    result = d.send("deadline_passed", deadline="worker", deadline_attempt=old)
    assert result.outcome == "refused"
    assert d.name == "diagnosing"
    assert d.fire("worker").state.name == "blocked"


def test_ac5_renewal_inside_the_same_state() -> None:
    d = Driver("fixing")
    old = d.state.armed_deadlines["worker"]
    d.send("commit_refused", cls="hook")
    assert d.name == "fixing"
    assert d.state.armed_deadlines["worker"] != old
    assert (
        d.send("deadline_passed", deadline="worker", deadline_attempt=old).outcome
        == "refused"
    )


def test_ac5_deadline_from_the_previous_attempt_is_refused() -> None:
    d = Driver("pushing")
    stale = d.state.armed_deadlines["worker"] - 1
    result = d.send("deadline_passed", deadline="worker", deadline_attempt=stale)
    assert result.outcome == "refused"
    assert d.name == "pushing"
    assert (
        d.send("deadline_passed", deadline="proof", deadline_attempt=1).outcome
        == "refused"
    )


def test_deadline_event_reads_the_arming_from_the_state() -> None:
    d = Driver("fixing")
    event = deadline_event("worker", T0, state=d.state)
    assert event.deadline == "worker"
    assert event.deadline_attempt == d.state.armed_deadlines["worker"]


# ----------------------------------------------------------------- help budget


@pytest.mark.parametrize("answer", ["retry", "cause"])
def test_help_is_bounded(answer: str) -> None:
    budget = 3
    config = ModelLaneWorkReducerConfig(help_budget=budget, help_tiers=TIERS)
    d = Driver(None, config)
    to_proving(d)
    episodes = 0
    for _ in range(budget + 2):
        if d.name == "blocked":
            break
        if d.name == "proving":
            d.fire("proof")
            if d.name == "help_requested":
                episodes += 1
        elif d.name == "red":
            d.send("log_read", red_kind="tests")
            d.send("cause_found")
            d.send("fix_committed")
            d.send("pushed", head_sha=HEAD)
            continue
        if d.name == "help_requested":
            if answer == "retry":
                d.send("help_retry", help_origin="proving")
            else:
                d.send("help_cause_found")
                d.send("fix_committed")
                d.send("pushed", head_sha=HEAD)
    assert d.name == "blocked"
    assert episodes == budget
    assert d.state.help_episodes == budget
    last = d.results[-1].intents[0]
    assert last.inputs["reason"] == "help_exhausted"


def test_help_climbs_the_chain_then_is_exhausted() -> None:
    d = Driver("diagnosing")
    d.send("cause_unknown")
    assert d.name == "help_requested"
    assert d.results[-1].intents[0].inputs["tier"] == "local"
    d.send("help_failed")
    assert d.results[-1].intents[0].inputs["tier"] == "glm-small"
    d.fire("help")
    assert d.results[-1].intents[0].inputs["tier"] == "claude-large"
    d.send("help_failed")
    assert d.name == "blocked"
    assert d.results[-1].intents[0].inputs["reason"] == "help_exhausted"


def test_first_responder_is_the_tier_above_the_worker() -> None:
    config = ModelLaneWorkReducerConfig(help_tiers=TIERS)
    d = Driver(None, config)
    d.send("start", worker_tier="glm-small")
    assert d.state.worker_tier == "glm-small"
    d.state = d.state.model_copy(update={"name": "diagnosing"})
    d.send("cause_unknown")
    assert d.results[-1].intents[0].inputs["tier"] == "claude-large"


def test_a_refused_answer_counts_as_help_failed() -> None:
    d = Driver("diagnosing")
    d.send("cause_unknown")
    result = d.send("help_retry", help_origin="diagnosing")
    assert result.outcome == "transition"
    assert result.transition_name is not None
    assert "help_failed" in result.transition_name
    assert d.results[-1].intents[0].inputs["tier"] == "glm-small"


# -------------------------------------------------------------------------- AC6


def thirty_events() -> list[tuple[str, dict[str, Any]]]:
    return [
        ("start", {"worker_tier": "claude-medium"}),
        ("worker_started", {"lane_name": "lane-x"}),
        ("claim_confirmed", {}),
        ("check_red", {"head_sha": HEAD, "observed_at": T0, "ci_read_at": T0}),
        ("log_read", {"red_kind": "tests"}),
        ("unclassified", {}),
        ("cause_found", {}),
        ("commit_refused", {"cls": "hook"}),
        ("fix_committed", {}),
        ("push_refused", {"cls": "check_failed"}),
        ("fix_committed", {}),
        ("head_moved", {"head_sha": OTHER}),
        ("fix_committed", {}),
        ("pushed", {"head_sha": OTHER}),
        ("proof_failed", {"cls": "tests"}),
        ("log_read", {"red_kind": "base_inherited"}),
        ("fix_committed", {}),
        ("pushed", {"head_sha": HEAD}),
        ("proof_passed", {"head_sha": HEAD}),
        ("check_green", {"head_sha": HEAD, "observed_at": T0, "ci_read_at": T0}),
        ("proof_posted", {}),
        ("handoff_written", {"head_sha": HEAD, "request_id": "pending"}),
        ("head_moved", {"head_sha": OTHER}),
        ("proof_passed", {"head_sha": OTHER}),
        ("proof_posted", {}),
        ("handoff_written", {"head_sha": OTHER, "request_id": "pending"}),
        (
            "check_green",
            {
                "head_sha": OTHER,
                "observed_at": "2026-11-26T11:00:00Z",
                "ci_read_at": "2026-11-26T11:00:00Z",
            },
        ),
        ("status_requested", {}),
        ("worker_started", {"lane_name": "again"}),
        ("check_green", {"head_sha": OTHER, "observed_at": T0, "ci_read_at": T0}),
    ]


def fold_sequence() -> tuple[list[str], list[str]]:
    d = Driver()
    events = thirty_events()
    assert len(events) == 30
    for kind, fields in events:
        if kind == "handoff_written":
            fields = dict(fields, request_id=d.state.handoff_request_id or "none")
        d.send(kind, now="2026-11-26T09:30:00Z", **fields)
    return (
        [r.state.model_dump_json() for r in d.results],
        [i.model_dump_json() for r in d.results for i in r.intents],
    )


def test_ac6_replay_is_byte_deterministic() -> None:
    first_states, first_intents = fold_sequence()
    second_states, second_intents = fold_sequence()
    assert len(first_states) == 30
    assert first_states == second_states
    assert first_intents == second_intents
    assert first_intents  # the sequence emits intents


def test_duplicate_input_folds_nothing() -> None:
    d = Driver()
    d.send("start", worker_tier="x", input_id="same")
    before = d.state
    result = d.send("start", worker_tier="x", input_id="same")
    assert result.outcome == "duplicate"
    assert d.state == before
    assert not result.intents


def test_status_requested_folds_nothing() -> None:
    d = Driver("red")
    before = d.state
    assert d.send("status_requested").outcome == "status"
    assert d.state == before


def test_repeat_bound_reached_is_never_sent_in() -> None:
    d = Driver("red")
    assert d.send("repeat_bound_reached").outcome == "refused"


def test_second_completion_for_one_action_is_refused() -> None:
    d = Driver()
    to_proving(d)
    action = d.outstanding("lab_proof")
    assert (
        d.send("proof_passed", head_sha=HEAD, action_id=action).outcome == "transition"
    )
    assert d.send("proof_passed", head_sha=HEAD, action_id=action).outcome == "refused"


def test_handle_wraps_the_fold() -> None:
    handler = HandlerLaneWorkReducer()
    state = handler.initial_state()
    request = ModelLaneWorkReduceRequest(
        state=state, event=ModelLaneWorkFoldInput(kind="start", worker_tier="t"), now=T0
    )
    result = handler.handle(request)
    assert result.state.name == "awaiting_worker"
    assert result.intents[0].name == "arm_worker_deadline"
    assert result.intents[0].deadline_at is not None


# -------------------------------------------------------------------------- AC7


def test_ac7_answer_for_an_old_request_does_not_end_the_workflow() -> None:
    d = Driver()
    to_handing_off(d)
    result = d.send("handoff_written", head_sha=HEAD, request_id="an-old-request")
    assert result.outcome == "refused"
    assert d.name == "handing_off"


def test_ac7_handed_off_answer_naming_another_head_does_not_end_it() -> None:
    d = Driver()
    to_handing_off(d)
    request = d.state.handoff_request_id
    result = d.send("handoff_written", head_sha=OTHER, request_id=request)
    assert result.outcome == "refused"
    assert d.name == "handing_off"


def test_ac7_observation_of_another_head_after_the_answer_ends_in_proving() -> None:
    d = Driver()
    to_handing_off(d)
    request = d.state.handoff_request_id
    d.send(
        "handoff_written", now="2026-11-26T09:30:00Z", head_sha=HEAD, request_id=request
    )
    assert d.name == "confirming_handoff"
    d.send(
        "head_moved",
        head_sha=OTHER,
        observed_at="2026-11-26T10:00:00Z",
        ci_read_at="2026-11-26T10:00:00Z",
    )
    assert d.name == "proving"
    assert d.name != "handed_off"
    assert d.state.expected_head == OTHER
    # an observation made before the answer does not confirm the handoff either
    e = Driver()
    to_handing_off(e)
    e.send(
        "handoff_written",
        now="2026-11-26T09:30:00Z",
        head_sha=HEAD,
        request_id=e.state.handoff_request_id,
    )
    stale = e.send(
        "check_green",
        head_sha=HEAD,
        observed_at="2026-11-26T09:00:00Z",
        ci_read_at="2026-11-26T09:00:00Z",
    )
    assert stale.outcome == "refused"
    assert e.name == "confirming_handoff"
