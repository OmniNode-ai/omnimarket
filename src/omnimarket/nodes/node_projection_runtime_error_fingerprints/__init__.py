# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_runtime_error_fingerprints — ranked error truth (OMN-18770)."""

from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_projection_runtime_error_fingerprints import (
    HandlerProjectionRuntimeErrorFingerprints,
)

__all__ = [
    "HandlerProjectionRuntimeErrorFingerprints",
    "NodeProjectionRuntimeErrorFingerprints",
]


class NodeProjectionRuntimeErrorFingerprints(HandlerProjectionRuntimeErrorFingerprints):
    """ONEX entry-point wrapper for HandlerProjectionRuntimeErrorFingerprints."""
