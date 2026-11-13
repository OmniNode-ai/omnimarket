# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of the branch deploy orchestrator to the nodes it composes (OMN-24027).

Each seam is a topic shared with another node. The test reads that node's own contract, so a topic or
payload renamed on either side fails here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_branch_deploy_command_effect.models.model_branch_deploy_command import (
    ModelBranchDeployCommandRequest,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.models import (
    ModelDeployJobCompleted,
    ModelDeployJobRejected,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.models.model_branch_deploy_probe import (
    ModelBranchDeployProbeRequest,
)
from omnimarket.nodes.node_host_install_state_reducer.handlers.handler_host_install_state import (
    HostInstallTransitionError,
    fold,
)
from omnimarket.nodes.node_host_install_state_reducer.models import (
    ModelHostInstallStepEvent,
)
from tests.nodes.node_branch_deploy_orchestrator.support import (
    FLOWS,
    reducer_overlay,
    run_flow,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
GOLDEN = Path(__file__).resolve().parent / "deploy_agent_job_events.json"


def contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_s1_orchestrator_reducer_seam() -> None:
    reducer = contract("node_host_install_state_reducer")["event_bus"]
    orch = contract("node_branch_deploy_orchestrator")["event_bus"]
    assert reducer["subscribe_topics"][0] == "onex.evt.omnimarket.host-install-step.v1"
    assert reducer["publish_topics"][0] in orch["subscribe_topics"]


def test_s1_control_out_of_order_step_raises() -> None:
    overlay = reducer_overlay()
    step = ModelHostInstallStepEvent(
        correlation_id=uuid4(),
        host="host-a",
        unit="dev-runtime-hotpatch",
        actor="lane-test",
        step="snapshot",
        outcome="ok",
        at=datetime(2026, 11, 12, 12, 0, tzinfo=UTC),
        detail="",
    )
    with pytest.raises(HostInstallTransitionError):
        fold(None, step, overlay)


def test_s2_orchestrator_command_probe_seam() -> None:
    orch = contract("node_branch_deploy_orchestrator")["event_bus"]
    cmd_doc = contract("node_branch_deploy_command_effect")
    probe_doc = contract("node_branch_deploy_probe_effect")
    cmd = cmd_doc["event_bus"]
    probe = probe_doc["event_bus"]
    assert (
        "onex.cmd.omnimarket.branch-deploy-command-requested.v1"
        in orch["publish_topics"]
    )
    assert (
        "onex.cmd.omnimarket.branch-deploy-command-requested.v1"
        in cmd["subscribe_topics"]
    )
    assert (
        "onex.cmd.omnimarket.branch-deploy-probe-requested.v1" in orch["publish_topics"]
    )
    assert (
        "onex.cmd.omnimarket.branch-deploy-probe-requested.v1"
        in probe["subscribe_topics"]
    )
    assert cmd_doc["terminal_event"] in orch["subscribe_topics"]
    assert cmd_doc["terminal_event"] in cmd["publish_topics"]
    assert probe_doc["terminal_event"] in orch["subscribe_topics"]
    assert probe_doc["terminal_event"] in probe["publish_topics"]


def test_s2_published_requests_are_the_effects_input_models() -> None:
    plant = run_flow("wrong_build")
    seen = {type(payload).__name__ for _, payload in plant.log}
    assert {"ModelBranchDeployCommandRequest", "ModelBranchDeployProbeRequest"} <= seen
    for topic, payload in plant.log:
        if topic.endswith("branch-deploy-command-requested.v1"):
            assert isinstance(payload, ModelBranchDeployCommandRequest)
        if topic.endswith("branch-deploy-probe-requested.v1"):
            assert isinstance(payload, ModelBranchDeployProbeRequest)


def test_s3_deploy_agent_job_terminal_events() -> None:
    raw = json.loads(GOLDEN.read_text())
    success = ModelDeployJobCompleted.model_validate(raw["completed_success"])
    assert success.status == "success"
    assert "runtime" in success.phase_results
    failed = ModelDeployJobCompleted.model_validate(raw["completed_failed"])
    assert failed.status == "failed"
    rejected = ModelDeployJobRejected.model_validate(raw["rejected"])
    assert rejected.reason == "busy"
    orch = contract("node_branch_deploy_orchestrator")
    assert orch["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.branch-deploy-requested.v1",
        "onex.evt.omnimarket.branch-deploy-command-completed.v1",
        "onex.evt.omnimarket.branch-deploy-probe-completed.v1",
        "onex.evt.omnimarket.host-install-state-updated.v1",
        "onex.evt.deploy.rebuild-completed.v1",
        "onex.evt.deploy.rebuild-rejected.v1",
    ]


def test_s3_control_missing_status_key_refused() -> None:
    raw = json.loads(GOLDEN.read_text())
    broken = dict(raw["completed_success"])
    broken.pop("status")
    with pytest.raises(ValidationError):
        ModelDeployJobCompleted.model_validate(broken)


def test_s3_control_renamed_status_key_refused() -> None:
    raw = json.loads(GOLDEN.read_text())
    broken = dict(raw["completed_success"])
    broken.pop("status")
    broken["verdict"] = "success"
    with pytest.raises(ValidationError):
        ModelDeployJobCompleted.model_validate(broken)


@pytest.mark.parametrize("flow", sorted(FLOWS))
def test_s1_all_flows_fold_without_reducer_error(flow: str) -> None:
    plant = run_flow(flow)
    assert plant.reducer_errors == []
    assert plant.completed is not None
