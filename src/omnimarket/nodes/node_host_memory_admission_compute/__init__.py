# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_memory_admission_compute — pure compute policy (OMN-17427)."""

from omnimarket.nodes.node_host_memory_admission_compute.handlers.handler_host_memory_admission import (
    HandlerHostMemoryAdmission,
)


class NodeHostMemoryAdmissionCompute(HandlerHostMemoryAdmission):
    """ONEX entry-point wrapper for HandlerHostMemoryAdmission."""


__all__ = ["HandlerHostMemoryAdmission", "NodeHostMemoryAdmissionCompute"]
