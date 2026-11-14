# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic provision plan compute: which broker topics a runtime must provision.

``HandlerTopicProvisionPlan.handle(ModelTopicProvisionRequest) ->
ModelTopicProvisionPlan`` (definition-B). A topic is provisioned only for a
contract the running runtime profile actually loads, instead of every contract
installed. Pure and deterministic; no I/O in the handler.
"""

from omnimarket.nodes.node_topic_provision_plan_compute.handlers.handler_topic_provision_plan import (
    HandlerTopicProvisionPlan,
    plan_topic_provision,
)


class NodeTopicProvisionPlanCompute(HandlerTopicProvisionPlan):
    """ONEX entry-point wrapper for HandlerTopicProvisionPlan."""


__all__ = [
    "HandlerTopicProvisionPlan",
    "NodeTopicProvisionPlanCompute",
    "plan_topic_provision",
]
