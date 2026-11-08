# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 15 synthetic delegated task verdict compute (OMN-22977)."""

from omnimarket.nodes.node_delegation_synthetic_probe_compute.handlers.handler_delegation_synthetic_probe import (
    HandlerDelegationSyntheticProbe,
)


class NodeDelegationSyntheticProbeCompute(HandlerDelegationSyntheticProbe):
    """ONEX entrypoint for the synthetic delegated task verdict."""


__all__ = ["HandlerDelegationSyntheticProbe", "NodeDelegationSyntheticProbeCompute"]
