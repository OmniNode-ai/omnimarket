# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR landing tick observe compute: the node path proven beside the live landing controller.

``HandlerPrLandingTickObserve.handle(ModelLandingTickObservation) -> ModelLandingTickComparison``
(definition-B). It replays a copy of one real tick's facts through the node path and reports every
path where the node's decision differs from the decision the live controller made.
"""

from omnimarket.nodes.node_pr_landing_tick_observe_compute.handlers.handler_pr_landing_tick_observe import (
    HandlerPrLandingTickObserve,
    diff_paths,
)


class NodePrLandingTickObserveCompute(HandlerPrLandingTickObserve):
    """ONEX entry-point wrapper for HandlerPrLandingTickObserve."""


__all__ = [
    "HandlerPrLandingTickObserve",
    "NodePrLandingTickObserveCompute",
    "diff_paths",
]
