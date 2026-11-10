# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dev roll request compute: batch dev_tip_changed events into one roll decision.

Shadow only (five whys plan R4): it records the roll it would request and sends
nothing. ``HandlerDevRollRequest.handle(ModelDevRollFacts)`` is pure.
"""

from omnimarket.nodes.node_dev_roll_request_compute.handlers.handler_dev_roll_request import (
    HandlerDevRollRequest,
    decide_dev_roll,
)


class NodeDevRollRequestCompute(HandlerDevRollRequest):
    """The node class the runtime loads; the handler carries the decision."""


__all__ = ["HandlerDevRollRequest", "NodeDevRollRequestCompute", "decide_dev_roll"]
