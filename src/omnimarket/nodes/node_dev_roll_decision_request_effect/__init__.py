# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_dev_roll_decision_request_effect (OMN-24251).

Shadow only (parent OMN-23532): node_dev_roll_request_compute is subscribed to
the decision-request topic but nothing publishes it. This node turns every
queued dev roll the deploy agent accepts into one
:class:`ModelDevRollFacts` published on that topic -- one pending
``dev_tip_changed`` event per accepted manifest, plus the shadow last roll --
and records what every observed decision cleared. It publishes no deploy-agent
command.
"""

from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers import (
    HandlerDevRollDecidedObserved,
    HandlerDevRollQueuedRoll,
)

__all__ = [
    "HandlerDevRollDecidedObserved",
    "HandlerDevRollQueuedRoll",
]
