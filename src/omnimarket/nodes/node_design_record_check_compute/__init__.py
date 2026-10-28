# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_design_record_check_compute: the R9 design record checker (OMN-21460)."""

from omnimarket.nodes.node_design_record_check_compute.handlers.handler_design_record_check import (
    HandlerDesignRecordCheck,
)

__all__ = ["HandlerDesignRecordCheck", "NodeDesignRecordCheckCompute"]


class NodeDesignRecordCheckCompute(HandlerDesignRecordCheck):
    """ONEX entry-point wrapper for HandlerDesignRecordCheck."""
