# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative node entrypoint; routing is declared by contract."""

from omnimarket.nodes.node_error_class_normalize_compute.handlers.handler_error_class_normalize import (
    HandlerErrorClassNormalize,
)


class NodeErrorClassNormalizeCompute(HandlerErrorClassNormalize):
    """Normalize caller-supplied facts without reading live state."""
