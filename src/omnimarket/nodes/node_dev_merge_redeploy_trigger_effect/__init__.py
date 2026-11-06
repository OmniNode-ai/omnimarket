# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab dev-merge redeploy trigger and verify, one serve process on a lab host (OMN-22478, R2)."""

from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers import (
    DevMergeRedeployHost,
    HandlerDevMergeRedeployTrigger,
    HandlerDevMergeRedeployVerify,
    correlation_id_for,
    load_dev_merge_redeploy_topics,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models import (
    EnumDevMergeOutcome,
    ModelDevMergeRedeployReceipt,
    ModelDevMergeRedeployTopics,
)


class NodeDevMergeRedeployTriggerEffect(HandlerDevMergeRedeployTrigger):
    """ONEX entry-point wrapper for HandlerDevMergeRedeployTrigger."""


__all__ = [
    "DevMergeRedeployHost",
    "EnumDevMergeOutcome",
    "HandlerDevMergeRedeployTrigger",
    "HandlerDevMergeRedeployVerify",
    "ModelDevMergeRedeployReceipt",
    "ModelDevMergeRedeployTopics",
    "NodeDevMergeRedeployTriggerEffect",
    "correlation_id_for",
    "load_dev_merge_redeploy_topics",
]
