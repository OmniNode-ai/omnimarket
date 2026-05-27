# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRunnerOrchestrator — GitHub Actions runner management orchestrator.

ONEX node type: ORCHESTRATOR — impure, effectful, SSH-backed runner operations.

Wave 4: contract + stub only.  Full implementation deferred (OMN-12218).
The handler class is importable and passes type checks; `handle()` raises
NotImplementedError as declared by `node_not_implemented: true` in contract.yaml.

Runner actions (per runner/SKILL.md):
  deploy  — SSH to CI host (192.168.86.201), invoke deploy-runners.sh.  # onex-allow-internal-ip OMN-12218 reason="runner orchestrator contract docstring; implementation remains deferred"
             Prerequisites: gh token with org scope + SSH key loaded in agent.
  update  — Force Docker image rebuild then redeploy (--rebuild flag).
  status  — GitHub API runner list + SSH Docker-label inspect + host disk metrics.
             Alerts: offline >5m, disk >=70%, runner version >2 releases behind.
"""

from __future__ import annotations

from omnimarket.nodes.node_runner_orchestrator.models.model_runner_request import (
    ModelRunnerRequest,
)
from omnimarket.nodes.node_runner_orchestrator.models.model_runner_result import (
    ModelRunnerResult,
)


class HandlerRunnerOrchestrator:
    """ORCHESTRATOR — GitHub Actions self-hosted runner deploy/update/status.

    Wave 4 contract-first node: importable and type-safe.  Full implementation
    deferred (OMN-12218).

    Per contract.yaml `node_not_implemented: true`, `handle()` raises
    NotImplementedError.  Callers should check the contract flag before invoking.
    """

    def handle(
        self,
        request: ModelRunnerRequest,
    ) -> ModelRunnerResult:  # stub-ok
        """Execute the requested runner action.

        Raises:
            NotImplementedError: contract.yaml node_not_implemented=true, Wave 4 in OMN-12218.
        """
        raise NotImplementedError(  # stub-ok
            "node_runner_orchestrator is a Wave 4 contract-first node. "
            "Full implementation is tracked in OMN-12218. "
            "See contract.yaml `node_not_implemented: true`."
        )
