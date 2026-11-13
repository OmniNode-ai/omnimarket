# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host hygiene decision compute: per-host, per-cleanup due decisions (OMN-23707).

``HandlerHostHygieneDecision.handle(ModelHostHygieneDecisionRequest) -> ModelHostHygieneDecision``
(definition-B). No clock (``now`` is an input), no I/O, no hard-coded threshold.
"""

from omnimarket.nodes.node_host_hygiene_decision_compute.handlers.handler_host_hygiene_decision import (
    HandlerHostHygieneDecision,
    decide_host_hygiene,
)


class NodeHostHygieneDecisionCompute(HandlerHostHygieneDecision):
    """ONEX entry-point wrapper for HandlerHostHygieneDecision."""


__all__ = [
    "HandlerHostHygieneDecision",
    "NodeHostHygieneDecisionCompute",
    "decide_host_hygiene",
]
