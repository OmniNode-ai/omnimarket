# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract's topics are the ones the node means to read and write (OMN-26721)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lane_work_orchestrator.event_topics import topic_roles

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"


def _contract(node: str) -> dict[str, object]:
    path = NODES / node / "contract.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_the_roles_name_exactly_these_topics() -> None:
    assert topic_roles() == {
        "ingress": "onex.cmd.omnimarket.lane-work-event.v1",
        "step_completed": "onex.evt.omnimarket.lane-work-step-completed.v1",
        "transition": "onex.evt.omnimarket.lane-work-transition.v1",
        "terminal": "onex.evt.omnimarket.lane-work-terminal.v1",
        "ledger_append": "onex.cmd.omnimarket.work-ledger-append-requested.v1",
        "ledger_query": "onex.cmd.omnimarket.work-ledger-query-requested.v1",
        "lab_proof": "onex.cmd.omnimarket.lab-work-unit-requested.v1",
        "repo_write": "onex.cmd.omnimarket.github-repo-write-requested.v1",
        "handoff": "onex.cmd.omnimarket.pr-handoff-requested.v1",
        "help_requested": "onex.cmd.omnimarket.lane-work-help-requested.v1",
    }


def test_every_subscribed_topic_has_a_handler_routing_entry() -> None:
    contract = _contract("node_lane_work_orchestrator")
    subscribed = set(contract["event_bus"]["subscribe_topics"])  # type: ignore[index]
    routed = {entry["topic"] for entry in contract["handler_routing"]["handlers"]}  # type: ignore[index]
    assert subscribed - routed == set()


def test_the_lab_proof_failure_is_routed_to_the_intake() -> None:
    contract = _contract("node_lane_work_orchestrator")
    routes = {
        entry["topic"]: entry["handler"]["name"]
        for entry in contract["handler_routing"]["handlers"]  # type: ignore[index]
    }
    assert (
        routes["onex.evt.omnimarket.lab-work-unit-failed.v1"] == "HandlerLaneWorkIntake"
    )


def test_the_reducer_declares_who_calls_it() -> None:
    reducer = _contract("node_lane_work_reducer")
    callers = [c["path"] for c in reducer["runtime_dispatch"]["callers"]]  # type: ignore[index]
    assert (
        "src/omnimarket/nodes/node_lane_work_orchestrator/orchestration/lane_work_orchestrator.py"
        in callers
    )


def test_the_projection_declares_its_applied_event() -> None:
    projection = _contract("node_projection_lane_work")
    published = projection["event_bus"]["publish_topics"]  # type: ignore[index]
    assert "onex.evt.omnimarket.projection-lane-work-applied.v1" in published


def test_a_lab_proof_failure_becomes_a_lab_result_for_the_workflow_that_asked() -> None:
    from omnimarket.lab_work.bus import ModelLabWorkCommandFailure
    from omnimarket.nodes.node_lane_work_orchestrator.handlers.handler_lane_work_intake import (
        HandlerLaneWorkIntake,
    )

    from .rig_lane_work import HEAD, Rig

    rig = Rig()
    rig.to_proving()
    proof = rig.outstanding("lab_proof")
    assert proof is not None
    failure = ModelLabWorkCommandFailure(
        work_unit_id=proof, commit_sha=HEAD, error_message="no host"
    )
    inputs = HandlerLaneWorkIntake(rig.orchestrator).handle(failure)
    assert [i.result_kind for i in inputs] == ["lab_work_unit_result"]
    payload = inputs[0].payload
    assert payload is not None
    assert payload["status"] == "failed"
    assert inputs[0].workflow_id == rig.workflow_id


def test_the_step_reply_and_the_terminal_are_declared_as_read_by_callers() -> None:
    contract = _contract("node_lane_work_orchestrator")
    assert set(contract["externally_consumed_topics"]) >= {  # type: ignore[call-overload]
        "onex.evt.omnimarket.lane-work-step-completed.v1",
        "onex.evt.omnimarket.lane-work-terminal.v1",
    }
