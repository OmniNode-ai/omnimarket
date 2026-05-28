# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRunnerOrchestrator — GitHub Actions runner management orchestrator.

ONEX node type: ORCHESTRATOR — impure, effectful, SSH-backed runner operations.

Bounded production slice: dry-run previews runner actions and all live runner
operations require an injected adapter.

Runner actions (per runner/SKILL.md):
  deploy  — SSH to CI host (192.168.86.201), invoke deploy-runners.sh.  # onex-allow-internal-ip OMN-12218 reason="runner orchestrator contract docstring; implementation remains deferred"
             Prerequisites: gh token with org scope + SSH key loaded in agent.
  update  — Force Docker image rebuild then redeploy (--rebuild flag).
  status  — GitHub API runner list + SSH Docker-label inspect + host disk metrics.
             Alerts: offline >5m, disk >=70%, runner version >2 releases behind.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_runner_orchestrator.models.model_runner_request import (
    EnumRunnerAction,
    ModelRunnerRequest,
)
from omnimarket.nodes.node_runner_orchestrator.models.model_runner_result import (
    EnumRunnerActionStatus,
    ModelRunnerResult,
)


class ProtocolRunnerAdapter(Protocol):
    """Adapter boundary for live runner deploy/update/status operations."""

    def deploy(
        self, request: ModelRunnerRequest, *, rebuild: bool
    ) -> ModelRunnerResult: ...

    def status(self, request: ModelRunnerRequest) -> ModelRunnerResult: ...


class HandlerRunnerOrchestrator:
    """ORCHESTRATOR — GitHub Actions self-hosted runner deploy/update/status.

    Dry-run never opens SSH or calls the GitHub API. Live operations are delegated
    to ``ProtocolRunnerAdapter``.
    """

    def __init__(self, adapter: ProtocolRunnerAdapter | None = None) -> None:
        self._adapter = adapter

    def handle(
        self,
        request: ModelRunnerRequest,
    ) -> ModelRunnerResult:
        """Execute the requested runner action.

        Raises:
            RuntimeError: when a live runner operation is requested without an adapter.
        """
        if request.dry_run:
            return ModelRunnerResult(
                action_status=EnumRunnerActionStatus.DRY_RUN,
                runners=[],
                host_metrics=None,
                actions_taken=[],
                dry_run_summary=_dry_run_summary(request),
                error=None,
                correlation_id=request.correlation_id,
            )

        if self._adapter is None:
            raise RuntimeError("runner adapter required when dry_run is false")

        if request.action is EnumRunnerAction.STATUS:
            return self._adapter.status(request)
        return self._adapter.deploy(
            request,
            rebuild=request.action is EnumRunnerAction.UPDATE,
        )


def _dry_run_summary(request: ModelRunnerRequest) -> str:
    target = request.runner_name or "all runners"
    if request.action is EnumRunnerAction.UPDATE:
        return f"would rebuild runner image and redeploy {target}"
    if request.action is EnumRunnerAction.DEPLOY:
        return f"would deploy cached runner image to {target}"
    return f"would query GitHub runner status and host metrics for {target}"


__all__ = ["HandlerRunnerOrchestrator", "ProtocolRunnerAdapter"]
