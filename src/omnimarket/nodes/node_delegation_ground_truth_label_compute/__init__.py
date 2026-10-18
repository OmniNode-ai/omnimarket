# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation ground-truth label compute node (OMN-19787)."""

from omnimarket.nodes.node_delegation_ground_truth_label_compute.handlers.handler_ground_truth_label import (
    HandlerGroundTruthLabel,
)


class NodeDelegationGroundTruthLabelCompute(HandlerGroundTruthLabel):
    """ONEX entrypoint for labelling delegated runs from independent evidence."""


__all__ = ["HandlerGroundTruthLabel", "NodeDelegationGroundTruthLabelCompute"]
