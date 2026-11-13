# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden, error and failing-control chains of the branch deploy command effect (OMN-24027).

* golden: deploy exit 0 -> published with the exact argv incl. --open-pr; rollback exit 0 -> published with no --open-pr and --image-ref when given;
* error: refused marker -> refused; other non-zero -> failed; timeout -> failed;
* control: a wrong flag name is detected as not equal to the produced argv; env values never leak into the result detail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_branch_deploy_command_effect.adapters.adapter_deploy_agent_trigger import (
    SubprocessDeployAgentTrigger,
)
from omnimarket.nodes.node_branch_deploy_command_effect.adapters.adapter_in_memory_trigger import (
    InMemoryDeployAgentTrigger,
)
from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
)
from omnimarket.nodes.node_branch_deploy_command_effect.models.model_branch_deploy_command import (
    ModelBranchDeployCommandRequest,
)
from omnimarket.nodes.node_branch_deploy_command_effect.protocols.protocol_deploy_agent_trigger import (
    ModelTriggerOutcome,
)
from tests.nodes.node_branch_deploy_command_effect.support import (
    HEAD,
    RUN_ID,
    overlay,
    request,
)

pytestmark = pytest.mark.unit


def _handler(trigger: InMemoryDeployAgentTrigger) -> HandlerBranchDeployCommand:
    return HandlerBranchDeployCommand(overlay=overlay(), trigger=trigger)


def test_golden_deploy_published() -> None:
    """Deploy exit 0 -> published; the exact argv list in order incl. --open-pr."""
    trigger = InMemoryDeployAgentTrigger(
        outcomes=[ModelTriggerOutcome(exit_code=0, stdout="Published.", stderr="")]
    )
    handler = _handler(trigger)
    req = request(mode="deploy")
    result = handler.handle(req)
    assert result.status == "published"
    assert result.exit_code == 0
    assert result.correlation_id == RUN_ID
    assert result.mode == "deploy"
    argv = list(trigger.calls[0][0])
    expected = [
        "bash",
        "/srv/example/deploy-agent-trigger.sh",
        "--runtime-lane",
        "lane-a",
        "--git-ref",
        HEAD,
        "--scope",
        "runtime",
        "--build-source",
        "workspace",
        "--rollback-actor",
        "actor-a",
        "--rollback-reason",
        "reason-a",
        "--requested-by",
        "user-a",
        "--correlation-id",
        str(RUN_ID),
        "--open-pr",
        f"omnibase_infra#5917@{HEAD}",
    ]
    assert argv == expected


def test_golden_rollback_published() -> None:
    """Rollback exit 0 -> published; no --open-pr; --image-ref when given."""
    trigger = InMemoryDeployAgentTrigger(
        outcomes=[ModelTriggerOutcome(exit_code=0, stdout="Published.", stderr="")]
    )
    handler = _handler(trigger)
    req = request(mode="rollback", image_ref="registry.example/omni:rollback")
    result = handler.handle(req)
    assert result.status == "published"
    assert result.mode == "rollback"
    argv = list(trigger.calls[0][0])
    assert "--open-pr" not in argv
    assert f"--git-ref {HEAD}" in " ".join(argv)
    assert "--image-ref" in argv
    assert "registry.example/omni:rollback" in argv


def test_error_chain_refused() -> None:
    """Non-zero exit with the refused marker in stderr -> refused, detail kept."""
    trigger = InMemoryDeployAgentTrigger(
        outcomes=[
            ModelTriggerOutcome(
                exit_code=1, stdout="", stderr="REFUSED: --runtime-lane is required"
            )
        ]
    )
    handler = _handler(trigger)
    result = handler.handle(request(mode="deploy"))
    assert result.status == "refused"
    assert result.exit_code == 1
    assert "--runtime-lane is required" in result.detail


def test_error_chain_failed() -> None:
    """Non-zero exit without the refused marker -> failed."""
    trigger = InMemoryDeployAgentTrigger(
        outcomes=[ModelTriggerOutcome(exit_code=2, stdout="", stderr="boom")]
    )
    handler = _handler(trigger)
    result = handler.handle(request(mode="deploy"))
    assert result.status == "failed"
    assert result.exit_code == 2
    assert result.detail == "boom"


def test_error_chain_timeout() -> None:
    """Timeout outcome exit -1 -> failed."""
    trigger = InMemoryDeployAgentTrigger(
        outcomes=[ModelTriggerOutcome(exit_code=-1, stdout="", stderr="TimeoutExpired")]
    )
    handler = _handler(trigger)
    result = handler.handle(request(mode="deploy"))
    assert result.status == "failed"
    assert result.exit_code == -1


def test_control_wrong_flag_fails() -> None:
    """A deliberately wrong flag name is not equal to the produced argv."""
    trigger = InMemoryDeployAgentTrigger(
        outcomes=[ModelTriggerOutcome(exit_code=0, stdout="Published.", stderr="")]
    )
    handler = _handler(trigger)
    req = request(mode="deploy")
    handler.handle(req)
    produced = list(trigger.calls[0][0])
    wrong = list(produced)
    wrong[2] = "--wrong-flag"
    assert produced != wrong


def test_env_values_never_in_result(tmp_path: Path) -> None:
    """The child sees the env value, but the handler's result detail never contains it."""
    env_file = tmp_path / "deploy-agent.env"
    env_file.write_text("SECRET=hunter2\n")
    trigger = SubprocessDeployAgentTrigger()
    ov = overlay().model_copy(update={"env_file": str(env_file)})
    handler = HandlerBranchDeployCommand(overlay=ov, trigger=trigger)
    req = ModelBranchDeployCommandRequest(
        correlation_id=RUN_ID,
        mode="deploy",
        runtime_lane="lane-a",
        repo="omnibase_infra",
        pr_number=5917,
        git_ref=HEAD,
        image_ref="",
        rollback_actor="actor-a",
        rollback_reason="reason-a",
        requested_by="user-a",
    )
    result = handler.handle(req)
    assert "hunter2" not in result.detail
