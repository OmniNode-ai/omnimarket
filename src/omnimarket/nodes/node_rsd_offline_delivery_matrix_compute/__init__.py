# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Offline C0 route-contract delivery-matrix compute node (OMN-17984)."""

from omnimarket.nodes.node_rsd_offline_delivery_matrix_compute.handlers.handler_rsd_offline_delivery_matrix import (
    HandlerRsdOfflineDeliveryMatrix,
    validate_rsd_offline_delivery_matrix,
)
from omnimarket.nodes.node_rsd_offline_delivery_matrix_compute.models.model_rsd_offline_delivery_matrix import (
    ModelRsdOfflineDeliveryMatrixInput,
    ModelRsdOfflineDeliveryMatrixOutput,
)


class NodeRsdOfflineDeliveryMatrixCompute(HandlerRsdOfflineDeliveryMatrix):
    """ONEX entry-point wrapper for the offline-only C0 validator."""


__all__ = [
    "HandlerRsdOfflineDeliveryMatrix",
    "ModelRsdOfflineDeliveryMatrixInput",
    "ModelRsdOfflineDeliveryMatrixOutput",
    "NodeRsdOfflineDeliveryMatrixCompute",
    "validate_rsd_offline_delivery_matrix",
]
