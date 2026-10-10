# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GLM allowance compute: what is left of the Coding Plan's window and week, and the chain that leaves.

``HandlerGlmAllowance.handle(ModelGlmAllowanceRequest) -> ModelGlmAllowanceResult`` (definition-B). The GLM
rungs of a delegation chain consult it: near a cap the work goes to the next rung instead of failing, and
with allowance unused an approved class prefers GLM over the costlier rungs.
"""

from omnimarket.nodes.node_glm_allowance_compute.handlers.handler_glm_allowance import (
    HandlerGlmAllowance,
)


class NodeGlmAllowanceCompute(HandlerGlmAllowance):
    """ONEX entry-point wrapper for HandlerGlmAllowance."""


__all__ = ["HandlerGlmAllowance", "NodeGlmAllowanceCompute"]
