# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TC.5 exemplar registry reconciliation compute node (OMN-21128)."""

from omnimarket.nodes.node_exemplar_registry_reconcile_compute.handlers.handler_exemplar_registry_reconcile import (
    HandlerExemplarRegistryReconcile,
)


class NodeExemplarRegistryReconcileCompute(HandlerExemplarRegistryReconcile):
    """ONEX entrypoint for pure exemplar registry reconciliation."""


__all__ = ["HandlerExemplarRegistryReconcile", "NodeExemplarRegistryReconcileCompute"]
