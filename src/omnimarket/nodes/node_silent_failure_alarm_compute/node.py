# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative silent-failure alarm compute node (OMN-17427)."""

from omnibase_core.nodes.node_compute import NodeCompute
from pydantic import BaseModel


class NodeSilentFailureAlarmCompute(NodeCompute[BaseModel, BaseModel]):
    """All decision work is routed by contract.yaml to the alarm handlers."""
