# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host disk decision compute: what each lab host removes, and when to alarm (OMN-23866).

``HandlerHostDiskDecision.handle(ModelHostDiskDecisionRequest) -> ModelHostDiskDecision`` (definition-B). No clock
(``now`` is an input), no I/O, no value of policy in code: every keep-days, cap and floor is a field of the request.
"""

from omnimarket.nodes.node_host_disk_decision_compute.handlers.handler_host_disk_decision import (
    HandlerHostDiskDecision,
    decide_host_disk,
)


class NodeHostDiskDecisionCompute(HandlerHostDiskDecision):
    """ONEX entry-point wrapper for HandlerHostDiskDecision."""


__all__ = ["HandlerHostDiskDecision", "NodeHostDiskDecisionCompute", "decide_host_disk"]
