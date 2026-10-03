# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation size band compute node (OMN-20167)."""

from omnimarket.nodes.node_delegation_size_band_compute.handlers.handler_delegation_size_band import (
    HandlerDelegationSizeBand,
)


class NodeDelegationSizeBandCompute(HandlerDelegationSizeBand):
    """ONEX entrypoint for deterministic text size measurement."""


__all__ = ["HandlerDelegationSizeBand", "NodeDelegationSizeBandCompute"]
