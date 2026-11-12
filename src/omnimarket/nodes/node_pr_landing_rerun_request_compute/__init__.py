# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR landing rerun request decision: one request answered from the tick's facts, a pure function (OMN-23830).

``HandlerPrLandingRerunRequest.handle(ModelRerunDecideInput) -> ModelLandingRerunAnswer``. The landing tick
orchestrator calls it in process for each open request; it has no bus surface of its own.
"""

from omnimarket.nodes.node_pr_landing_rerun_request_compute.handlers.handler_pr_landing_rerun_request import (
    HandlerPrLandingRerunRequest,
)


class NodePrLandingRerunRequestCompute(HandlerPrLandingRerunRequest):
    """ONEX entry-point wrapper for HandlerPrLandingRerunRequest."""


__all__ = ["HandlerPrLandingRerunRequest", "NodePrLandingRerunRequestCompute"]
