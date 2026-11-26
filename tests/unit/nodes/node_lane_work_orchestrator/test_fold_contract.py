# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fold's contract: one receipt per accepted input, consecutive seq, no duplicate (OMN-26721)."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.lane_work import ModelLaneWorkChange

from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lane_work_orchestrator.agent_actions import (
    AGENT_ACTION_KINDS,
    worker_kind,
)
from omnimarket.nodes.node_lane_work_orchestrator.handlers.handler_lane_work_intake import (
    HandlerLaneWorkIntake,
)

from .rig_lane_work import HEAD, T0, Rig

pytestmark = pytest.mark.unit

NODE = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes"
REDUCER_CONTRACT = NODE / "node_lane_work_reducer/contract.yaml"
HANDLERS = NODE / "node_lane_work_orchestrator/handlers"


def test_every_accepted_input_has_one_receipt_with_consecutive_seq() -> None:
    rig = Rig()
    rig.to_handed_off()
    receipts = [r.record.receipt for r in rig.results if r.record is not None]
    assert [r.seq for r in receipts] == list(range(1, len(rig.sent) + 1))
    assert len({r.input_id for r in receipts}) == len(rig.sent)
    journal_ids = [
        output_id for output_id, _ in rig.orchestrator.journal.read("transition")
    ]
    assert journal_ids == [r.receipt_id for r in receipts]


def test_a_duplicate_input_writes_no_receipt_and_gets_the_current_reply() -> None:
    rig = Rig()
    rig.send("start", worker_tier="claude-medium")
    first = rig.sent[0]
    before = rig.orchestrator.journal.read("transition")
    again = first.model_copy(update={"correlation_id": uuid4()})
    result = rig.orchestrator.handle(again)
    assert result.outcome == "duplicate"
    assert result.record is None
    assert rig.orchestrator.journal.read("transition") == before
    assert result.reply is not None
    assert result.reply.correlation_id == again.correlation_id
    assert result.reply.reply.state.value == "awaiting_worker"


def test_two_starts_for_one_change_give_one_live_workflow_and_one_refusal() -> None:
    rig = Rig()
    rig.send("start", worker_tier="claude-medium")
    second = rig.send("start", worker_tier="claude-medium")
    assert second.outcome == "refused"
    assert len(rig.orchestrator.live_workflows()) == 1
    assert second.record is not None
    assert second.record.receipt.workflow_id == rig.workflow_id


def test_a_start_after_a_terminal_begins_the_next_attempt() -> None:
    rig = Rig()
    rig.send("start", worker_tier="claude-medium")
    state = rig.orchestrator.state_of(rig.workflow_id or "")
    rig.send(
        "deadline_passed",
        deadline_id="worker",
        deadline_attempt=state.armed_deadlines["worker"],
    )
    first = rig.workflow_id
    rig.send("start", worker_tier="claude-medium")
    assert rig.workflow_id != first
    assert (rig.workflow_id or "").endswith("-2")


def test_a_worker_that_stops_calling_ends_blocked_worker_lost_from_the_tick_alone() -> (
    None
):
    rig = Rig()
    rig.send("start", worker_tier="claude-medium")
    rig.send("worker_started", lane_name="lane-x")
    rig.send("claim_confirmed")
    rig.check("check_red", T0 + timedelta(minutes=5))
    rig.send("log_read", red_kind="tests")
    rig.send("cause_found")
    assert rig.name == "fixing"
    intake = HandlerLaneWorkIntake(rig.orchestrator)
    from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

    tick = ModelRuntimeTick(
        now=T0 + timedelta(days=1),
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=T0 + timedelta(days=1),
        correlation_id=uuid4(),
        scheduler_id="test",
        tick_interval_ms=1000,
    )
    for ingress in intake.handle(tick):
        rig.orchestrator.handle(ingress)
    assert rig.name == "blocked"
    reasons = [p["reason"] for _, p in rig.orchestrator.journal.read("terminal")]
    assert reasons == ["worker_lost"]


def test_a_proof_request_for_the_longest_workflow_id_is_a_valid_lab_request() -> None:
    change = ModelLaneWorkChange(
        owner="OmniNode-ai", repo="o" * 26, pr_number=123456, ticket="OMN-26721"
    )
    rig = Rig(change=change)
    rig.to_proving()
    assert len(rig.workflow_id or "") >= 40
    proofs = [p for _, p in rig.orchestrator.journal.read("lab_proof")]
    assert len(proofs) == 1
    request = ModelLabWorkUnitRequest.model_validate(proofs[0])
    assert request.work_unit_id.startswith(rig.workflow_id or "")
    assert request.commit_sha == HEAD
    assert 8 <= len(request.work_unit_id) <= 64


def test_a_worker_is_never_told_to_prove_post_or_hand_off() -> None:
    machine = yaml.safe_load(REDUCER_CONTRACT.read_text())["state_machine"]
    agent_actions = {
        action["action_name"]
        for transition in machine["transitions"]
        for action in transition.get("actions", [])
        if action["action_type"] == "agent"
    }
    assert agent_actions, "the reducer declares agent actions"
    assert {worker_kind(name) for name in agent_actions} <= AGENT_ACTION_KINDS
    assert {"prove", "post", "hand_off", "handoff"}.isdisjoint(AGENT_ACTION_KINDS)


def test_every_next_action_given_to_a_worker_is_in_the_allowed_kinds() -> None:
    rig = Rig()
    rig.send("start", worker_tier="claude-medium")
    rig.send("worker_started", lane_name="lane-x")
    rig.send("claim_confirmed")
    rig.check("check_red", T0 + timedelta(minutes=5))
    rig.send("log_read", red_kind="tests")
    rig.send("cause_found")
    rig.send("fix_committed")
    kinds = []
    for result in rig.results:
        assert result.reply is not None
        reply = result.reply.reply
        if reply.next_action == "agent_action":
            assert reply.action is not None
            assert reply.action.actor == "agent"
            kinds.append(reply.action.name)
    assert kinds
    assert set(kinds) <= AGENT_ACTION_KINDS


def test_no_address_host_or_topic_is_hard_coded_in_the_handlers() -> None:
    pattern = re.compile(r"onex\.(cmd|evt|intent)\.|h0[0-9]{2}|192\.168|:9092")
    offenders = [
        f"{path.name}:{number}: {line}"
        for path in sorted(HANDLERS.glob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []
    # positive control: the pattern does see a topic literal
    assert pattern.search("onex.cmd.x.v1")


def test_a_pr_observation_folds_as_check_green_through_the_classifier() -> None:
    from omnimarket.events.pr_state import EnumPrState, ModelPrStateObservedEvent

    rig = Rig()
    rig.send("start", worker_tier="claude-medium")
    rig.send("worker_started", lane_name="lane-x")
    rig.send("claim_confirmed")
    intake = HandlerLaneWorkIntake(rig.orchestrator)

    def observation(at: str) -> ModelPrStateObservedEvent:
        return ModelPrStateObservedEvent(
            repo="OmniNode-ai/omnimarket",
            pr_number=7,
            state=EnumPrState.OPEN,
            head_sha=HEAD,
            base="dev",
            head_ref="b",
            title="t",
            draft=True,
            author="a",
            author_is_bot=False,
            labels=(),
            armed=False,
            queued=False,
            watcher_class="green",
            ci_verdict="GREEN",
            red_contexts=(),
            pending_contexts=(),
            ci_read_at=at,
            merged_at="",
            observed_at=at,
        )

    fresh = observation("2026-11-26T09:30:00Z")
    (ingress,) = intake.handle(fresh)
    result = rig.orchestrator.handle(ingress)
    assert result.outcome == "transition"
    assert rig.name == "proving"
    assert result.record is not None
    assert result.record.receipt.event_kind.value == "check_green"
