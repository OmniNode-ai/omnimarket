# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment-fact gate: refuse a new deployment fact in omnimarket's packaged routing configs.

``HandlerDeploymentFactGate.handle(ModelDeploymentFactGateRequest) -> ModelDeploymentFactGateResult``
(OMN-20287). The marked fields are declared on the typed config models; the base revision is the
shrink-only baseline.
"""

from omnimarket.nodes.node_deployment_fact_gate_effect.handlers.handler_deployment_fact_gate import (
    HandlerDeploymentFactGate,
    judge_packaged_configs,
)


class NodeDeploymentFactGateEffect(HandlerDeploymentFactGate):
    """ONEX entry-point wrapper for HandlerDeploymentFactGate."""


__all__ = [
    "HandlerDeploymentFactGate",
    "NodeDeploymentFactGateEffect",
    "judge_packaged_configs",
]
