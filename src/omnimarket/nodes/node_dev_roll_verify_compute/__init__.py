# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dev roll verify compute: counts runtime starts per dev roll and verdicts the roll.

Five whys plan R5, decision D9 option (a): one runtime start per roll, with the
hot patch (branch install) inside that start. The node counts the runtime starts
of one finished dev roll and verdicts the roll against the declared starts per
roll (an overlay value). ``HandlerDevRollVerify.handle(ModelDevRollVerifyFacts)``
is pure.
"""

from omnimarket.nodes.node_dev_roll_verify_compute.handlers.handler_dev_roll_verify import (
    HandlerDevRollVerify,
    decide_dev_roll_verify,
)


class NodeDevRollVerifyCompute(HandlerDevRollVerify):
    """The node class the runtime loads; the handler carries the decision."""


__all__ = ["HandlerDevRollVerify", "NodeDevRollVerifyCompute", "decide_dev_roll_verify"]
