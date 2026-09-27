# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation output extract compute node (OMN-19600).

Pure COMPUTE: an accepted delegation reply plus the caller's declared output
files in; the accepted files and a manifest (path, kind, sha256, size,
artifact ref, refusals) out. Zero I/O; deterministic.
"""

from omnimarket.nodes.node_delegation_output_extract_compute.handlers.handler_delegation_output_extract import (
    HandlerDelegationOutputExtract,
    extract_declared_outputs,
)
from omnimarket.nodes.node_delegation_output_extract_compute.models.model_delegation_output_extract import (
    ModelDelegationOutputExtractRequest,
    ModelDelegationOutputExtractResult,
)


class NodeDelegationOutputExtractCompute(HandlerDelegationOutputExtract):
    """ONEX entry-point wrapper for HandlerDelegationOutputExtract."""


__all__ = [
    "HandlerDelegationOutputExtract",
    "ModelDelegationOutputExtractRequest",
    "ModelDelegationOutputExtractResult",
    "NodeDelegationOutputExtractCompute",
    "extract_declared_outputs",
]
