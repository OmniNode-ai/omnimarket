# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handler of node_branch_deploy_command_effect.

``HandlerBranchDeployCommand`` builds the deploy agent trigger argv from a typed request and the overlay, runs the
trigger through an injected port, and maps the outcome to a typed result. It is not a second signing or publishing
path: it only builds the trigger argv and runs the trigger.
"""

from __future__ import annotations

from omnimarket.nodes.node_branch_deploy_command_effect.adapters.adapter_deploy_agent_trigger import (
    SubprocessDeployAgentTrigger,
)
from omnimarket.nodes.node_branch_deploy_command_effect.models.model_branch_deploy_command import (
    ModelBranchDeployCommandRequest,
    ModelBranchDeployCommandResult,
)
from omnimarket.nodes.node_branch_deploy_command_effect.overlay import (
    ModelBranchDeployCommandOverlay,
    load_overlay,
)
from omnimarket.nodes.node_branch_deploy_command_effect.protocols.protocol_deploy_agent_trigger import (
    ModelTriggerOutcome,
    ProtocolDeployAgentTrigger,
)


class HandlerBranchDeployCommand:
    """Builds and runs the deploy agent trigger for a branch deploy command."""

    def __init__(
        self,
        overlay: ModelBranchDeployCommandOverlay | None = None,
        trigger: ProtocolDeployAgentTrigger | None = None,
    ) -> None:
        self._configured_overlay = overlay
        self._trigger = (
            trigger if trigger is not None else SubprocessDeployAgentTrigger()
        )

    @property
    def _overlay(self) -> ModelBranchDeployCommandOverlay:
        if self._configured_overlay is None:
            self._configured_overlay = load_overlay()
        return self._configured_overlay

    def _build_argv(self, request: ModelBranchDeployCommandRequest) -> list[str]:
        overlay = self._overlay
        argv: list[str] = list(overlay.trigger_argv_prefix)
        argv.extend(["--runtime-lane", request.runtime_lane])
        argv.extend(["--git-ref", request.git_ref])
        argv.extend(["--scope", overlay.scope])
        argv.extend(["--build-source", overlay.build_source])
        argv.extend(["--rollback-actor", request.rollback_actor])
        argv.extend(["--rollback-reason", request.rollback_reason])
        argv.extend(["--requested-by", request.requested_by])
        argv.extend(["--correlation-id", str(request.correlation_id)])
        if request.mode == "deploy":
            argv.extend(
                ["--open-pr", f"{request.repo}#{request.pr_number}@{request.git_ref}"]
            )
        if request.image_ref:
            argv.extend(["--image-ref", request.image_ref])
        return argv

    def handle(
        self, request: ModelBranchDeployCommandRequest
    ) -> ModelBranchDeployCommandResult:
        overlay = self._overlay
        argv = self._build_argv(request)
        outcome: ModelTriggerOutcome = self._trigger.run(
            argv,
            overlay.env_file,
            overlay.timeout_seconds,
        )
        if outcome.exit_code == 0:
            status = "published"
        elif outcome.stderr.startswith(overlay.refused_marker):
            status = "refused"
        else:
            status = "failed"
        detail = outcome.stderr if outcome.stderr else outcome.stdout
        detail = detail[: overlay.detail_max_chars]
        return ModelBranchDeployCommandResult(
            correlation_id=request.correlation_id,
            mode=request.mode,
            status=status,
            exit_code=outcome.exit_code,
            detail=detail,
        )
