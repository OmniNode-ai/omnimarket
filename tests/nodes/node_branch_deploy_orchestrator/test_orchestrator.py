# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The branch deploy orchestrator against the real reducer, command effect and probe effect (OMN-24027).

Every chain here is run through ``Plant``, which hands each published command to the node that owns the topic
and each published step to the real reducer fold. The reducer, not the test, decides the state each chain ends in.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_branch_deploy_orchestrator.handlers.handler_branch_deploy_orchestrator import (
    HandlerBranchDeployOrchestrator,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.models.model_branch_deploy_probe import (
    ModelBranchDeployProbeResult,
)
from omnimarket.nodes.node_host_install_state_reducer.models import EnumHostInstallState
from tests.nodes.node_branch_deploy_orchestrator.support import (
    HEAD,
    IMAGE,
    OTHER,
    ROLLBACK_HEAD,
    UNREADABLE,
    Plant,
    StepClock,
    job_event,
    orchestrator_overlay,
    published,
    read,
    refused,
    request,
    run_flow,
)

pytestmark = pytest.mark.unit

S = EnumHostInstallState
STEP = "onex.evt.omnimarket.host-install-step.v1"
COMMAND = "onex.cmd.omnimarket.branch-deploy-command-requested.v1"
PROBE = "onex.cmd.omnimarket.branch-deploy-probe-requested.v1"
DONE = "onex.evt.omnimarket.branch-deploy-completed.v1"


def test_golden_deployed() -> None:
    plant = run_flow("golden")
    assert plant.completed is not None
    assert plant.completed.outcome == "deployed"
    assert plant.completed.final_state is S.RUN_OK
    assert plant.completed.observed_sha == HEAD
    assert plant.reducer_errors == []
    assert [t for t, _ in plant.log] == [
        STEP,
        STEP,
        STEP,
        COMMAND,
        STEP,
        PROBE,
        STEP,
        STEP,
        DONE,
    ]
    assert [r.state for r in plant.records] == [
        S.REQUESTED,
        S.SNAPSHOTTED,
        S.INSTALLING,
        S.INSTALLED,
        S.VERIFIED,
        S.RUN_OK,
    ]
    assert len(plant.argvs) == 1
    assert "--open-pr" in plant.argvs[0]
    assert f"omnibase_infra#5917@{HEAD}" in plant.argvs[0]
    record = plant.records[-1]
    assert record.requested_by == "lane-test"
    assert record.host == "host-a"
    assert record.rollback_target_digest != record.render_digest


def test_golden_rollback_after_wrong_build() -> None:
    plant = run_flow("wrong_build")
    assert plant.completed is not None
    assert plant.completed.outcome == "rolled_back"
    assert plant.completed.final_state is S.ROLLED_BACK
    assert len(plant.argvs) == 2
    second = plant.argvs[1]
    assert "--open-pr" not in second
    assert second[second.index("--git-ref") + 1] == ROLLBACK_HEAD
    assert second[second.index("--image-ref") + 1] == IMAGE
    failed = [r for r in plant.records if r.state is S.FAILED]
    assert failed
    assert failed[0].failure_detail == "digest_mismatch"


def test_error_chain_unhealthy_runtime_rolls_back() -> None:
    plant = run_flow("unhealthy")
    assert plant.completed is not None
    assert plant.completed.outcome == "rolled_back"
    assert len(plant.argvs) == 2
    assert plant.reducer_errors == []


def test_error_chain_probe_unreadable_rolls_back() -> None:
    plant = run_flow("unreadable")
    assert plant.completed is not None
    assert plant.completed.outcome == "rolled_back"
    assert len(plant.argvs) == 2


def test_error_chain_deploy_command_refused() -> None:
    plant = run_flow("refused")
    assert plant.completed is not None
    assert plant.completed.outcome == "failed"
    assert plant.completed.final_state is S.FAILED
    assert len(plant.argvs) == 1
    assert plant.probe.calls == []
    assert "refused" in plant.completed.detail.lower()


def test_error_chain_job_failed_sends_no_rollback() -> None:
    plant = Plant(outcomes=[published()], job_events=["completed_failed"])
    plant.run(request())
    assert plant.completed is not None
    assert plant.completed.outcome == "failed"
    assert "runtime=failed" in plant.completed.detail
    assert len(plant.argvs) == 1
    assert plant.probe.calls == []


def test_error_chain_job_rejected() -> None:
    plant = Plant(outcomes=[published()], job_events=["rejected"])
    plant.run(request())
    assert plant.completed is not None
    assert plant.completed.outcome == "failed"
    assert "rejected" in plant.completed.detail.lower()
    assert len(plant.argvs) == 1


def test_error_chain_rollback_command_refused() -> None:
    plant = Plant(
        outcomes=[published(), refused("no")],
        job_events=["completed_success"],
        probes=[read(OTHER, True)],
    )
    plant.run(request())
    assert plant.completed is not None
    assert plant.completed.outcome == "failed"
    assert plant.completed.rollback_failures == 1
    assert len(plant.argvs) == 2


def test_error_chain_rollback_job_failed() -> None:
    plant = Plant(
        outcomes=[published(), published()],
        job_events=["completed_success", "completed_failed"],
        probes=[read(OTHER, True)],
    )
    plant.run(request())
    assert plant.completed is not None
    assert plant.completed.outcome == "failed"
    assert plant.completed.rollback_failures == 1


def test_error_chain_rollback_lands_on_wrong_build() -> None:
    plant = Plant(
        outcomes=[published(), published()],
        job_events=["completed_success", "completed_success"],
        probes=[read(OTHER, True), read(OTHER, True, ROLLBACK_HEAD)],
    )
    plant.run(request())
    assert plant.completed is not None
    assert plant.completed.outcome == "failed"
    assert plant.completed.rollback_failures == 1
    assert plant.records[-1].failure_detail == "rollback_digest_mismatch"


def test_error_chain_rollback_probe_unreadable() -> None:
    plant = Plant(
        outcomes=[published(), published()],
        job_events=["completed_success", "completed_success"],
        probes=[read(OTHER, True), UNREADABLE],
    )
    plant.run(request())
    assert plant.completed is not None
    assert plant.completed.outcome == "failed"
    assert plant.completed.rollback_failures == 1


def test_unknown_correlation_ignored() -> None:
    orchestrator = HandlerBranchDeployOrchestrator(orchestrator_overlay(), StepClock())
    key = uuid4()
    assert orchestrator.decide(job_event("completed_success", key), key) == ()
    assert orchestrator.decide(job_event("rejected", key), key) == ()
    assert orchestrator.known_deploys == ()


def test_result_in_wrong_phase_ignored() -> None:
    orchestrator = HandlerBranchDeployOrchestrator(orchestrator_overlay(), StepClock())
    req = request()
    orchestrator.decide(req, req.correlation_id)
    probe_result = ModelBranchDeployProbeResult(
        correlation_id=req.correlation_id,
        phase="deploy",
        status="read",
        observed_sha=HEAD,
        healthy=True,
        expected_sha=HEAD,
    )
    assert orchestrator.decide(probe_result, req.correlation_id) == ()
    assert (
        orchestrator.decide(
            job_event("completed_success", req.correlation_id), req.correlation_id
        )
        == ()
    )


def test_control_rollback_is_not_sent_on_a_good_build() -> None:
    """The failing control of the rollback tests: the same flow with a wrong build sends a second trigger call."""
    good, bad = run_flow("golden"), run_flow("wrong_build")
    assert len(good.argvs) == 1
    assert len(bad.argvs) == 2
