# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative host state node."""

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.host_checks import (
    ModelHostCheckFinding,
    ModelInstallClaimRequest,
)


class NodeInstallClaimCheckCompute(
    NodeCompute[ModelInstallClaimRequest, ModelHostCheckFinding]
):
    """Handler, models, topics and dependencies are bound by the contract."""
