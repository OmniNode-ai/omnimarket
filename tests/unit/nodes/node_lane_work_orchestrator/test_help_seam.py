# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The help seam: entering help_requested publishes one message on the help topic and no ledger
MSG row of its own; a worker tier outside the chain refuses start (OMN-26721, plan 12.3, 12.4)."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.model_harness_backend_call import ModelHarnessBackendCallResult
from omnimarket.nodes.node_lane_work_orchestrator.event_topics import (
    ROLE_HELP_REQUESTED,
    ROLE_LEDGER_APPEND,
    topic_for,
)
from omnimarket.nodes.node_lane_work_orchestrator.handlers.handler_lane_work_intake import (
    HandlerLaneWorkIntake,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_help_message import (
    ModelLaneWorkHelpMessage,
)
from omnimarket.nodes.node_lane_work_orchestrator.orchestration.lane_work_orchestrator import (
    correlation_of_action,
)

from .rig_lane_work import NODES, T0, Rig, make_orchestrator

pytestmark = pytest.mark.unit

TIER_IDS = (
    "local",
    "glm-small",
    "glm-medium",
    "glm-large",
    "codex-cheapest",
    "claude-small",
    "claude-medium",
    "claude-large",
)


def _to_diagnosing(rig: Rig) -> None:
    rig.send("start", worker_tier="claude-medium")
    rig.send("worker_started", lane_name="lane-x", body_ticket_ids=("OMN-26721",))
    rig.send("claim_confirmed")
    rig.check("check_red", T0 + timedelta(minutes=5))
    rig.send("log_read", red_kind="other", evidence=("the log names no cause",))
    assert rig.name == "diagnosing"


def test_cause_unknown_publishes_one_help_message_and_no_ledger_message_row() -> None:
    rig = Rig()
    _to_diagnosing(rig)
    result = rig.send("cause_unknown", evidence=("five whys ended",))
    assert rig.name == "help_requested"
    roles = [output.role for output in result.published]
    assert roles.count(ROLE_HELP_REQUESTED) == 1
    ledger_rows = [
        output.payload["rows"]
        for output in result.published
        if output.role == ROLE_LEDGER_APPEND
    ]
    assert not any("| MSG |" in row for row in ledger_rows)
    message = ModelLaneWorkHelpMessage.model_validate(
        next(o.payload for o in result.published if o.role == ROLE_HELP_REQUESTED)
    )
    assert message.workflow_id == rig.workflow_id
    assert message.help_origin == "diagnosing"
    assert message.tier.name == "claude-large"  # the tier above a claude-medium worker
    assert message.worker_tier == "claude-medium"
    assert not message.no_responder
    assert message.action_id == next(
        o.output_id for o in result.published if o.role == ROLE_HELP_REQUESTED
    )


def test_the_help_topic_is_declared_by_the_contract() -> None:
    assert topic_for(ROLE_HELP_REQUESTED) == (
        "onex.cmd.omnimarket.lane-work-help-requested.v1"
    )
    contract = yaml.safe_load(
        (NODES / "node_lane_work_orchestrator/contract.yaml").read_text("utf-8")
    )
    assert topic_for(ROLE_HELP_REQUESTED) in contract["event_bus"]["publish_topics"]
    for topic in (
        "onex.evt.omnimarket.delegation-harness-completed.v1",
        "onex.evt.omnimarket.delegate-skill-failed.v1",
    ):
        assert topic in contract["event_bus"]["subscribe_topics"]


@pytest.mark.parametrize("tier", ["not-a-tier", "claude-medium-2"])
def test_a_start_with_a_worker_tier_outside_the_chain_is_refused_and_starts_nothing(
    tier: str,
) -> None:
    rig = Rig(make_orchestrator())
    result = rig.send("start", worker_tier=tier)
    assert result.outcome == "refused"
    assert result.record is None
    assert rig.orchestrator.known_workflows() == []
    assert result.refusal is not None
    assert "worker_tier_not_in_chain" in result.refusal
    assert all(name in result.refusal for name in TIER_IDS)
    answered = [o for o in result.published if o.role == "step_completed"]
    assert len(answered) == 1
    assert answered[0].payload["refusal"] == result.refusal


def test_an_accepted_start_still_creates_the_workflow() -> None:
    rig = Rig()
    result = rig.send("start", worker_tier="claude-medium")
    assert result.outcome == "transition"
    assert rig.name == "awaiting_worker"


def test_a_help_answer_is_matched_by_the_action_id_as_its_correlation_id() -> None:
    rig = Rig()
    _to_diagnosing(rig)
    result = rig.send("cause_unknown", evidence=("five whys ended",))
    action_id = next(
        o.output_id for o in result.published if o.role == ROLE_HELP_REQUESTED
    )
    intake = HandlerLaneWorkIntake(rig.orchestrator)
    ours = ModelHarnessBackendCallResult(
        request_id=correlation_of_action(action_id), status="refused", ok=False
    )
    theirs = ModelHarnessBackendCallResult(
        request_id=uuid4(), status="refused", ok=False
    )
    answered = intake.handle(ours)
    assert len(answered) == 1
    assert answered[0].result_kind == "delegation_harness_result"
    assert answered[0].payload["action_id"] == action_id
    assert intake.handle(theirs) == []  # a run the orchestrator did not start


def test_a_top_tier_worker_asking_for_help_is_marked_as_having_no_responder() -> None:
    rig = Rig()
    rig.send("start", worker_tier="claude-large")
    rig.send("worker_started", lane_name="lane-x", body_ticket_ids=("OMN-26721",))
    rig.send("claim_confirmed")
    rig.check("check_red", T0 + timedelta(minutes=5))
    rig.send("log_read", red_kind="other", evidence=("the log names no cause",))
    result = rig.send("cause_unknown", evidence=("five whys ended",))
    message = ModelLaneWorkHelpMessage.model_validate(
        next(o.payload for o in result.published if o.role == ROLE_HELP_REQUESTED)
    )
    assert message.no_responder
