# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR landing order waits compute: the merge-order waits of one landing tick, from parsed ledger rows.

``HandlerPrLandingOrderWaits.handle(ModelLandingOrderWaitsRequest) -> ModelLandingOrderWaitsResult``
(definition-B). It reads which open PRs a lane parked behind another PR, which of those waits fired, and
which fired waits a lane already routed.
"""

from omnimarket.nodes.node_pr_landing_order_waits_compute.handlers.handler_pr_landing_order_waits import (
    HandlerPrLandingOrderWaits,
    derive_order_waits,
)


class NodePrLandingOrderWaitsCompute(HandlerPrLandingOrderWaits):
    """ONEX entry-point wrapper for HandlerPrLandingOrderWaits."""


__all__ = [
    "HandlerPrLandingOrderWaits",
    "NodePrLandingOrderWaitsCompute",
    "derive_order_waits",
]
