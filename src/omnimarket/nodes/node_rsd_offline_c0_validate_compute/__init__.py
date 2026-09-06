# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Offline C0 validation node for canonical delegation-route evidence."""

from omnimarket.nodes.node_rsd_offline_c0_validate_compute.handlers.handler_rsd_offline_c0 import (
    HandlerRsdOfflineC0,
    validate_rsd_offline_c0,
)
from omnimarket.nodes.node_rsd_offline_c0_validate_compute.models.model_rsd_offline_c0 import (
    ModelRsdOfflineC0Input,
    ModelRsdOfflineC0Output,
)


class NodeRsdOfflineC0ValidateCompute(HandlerRsdOfflineC0):
    """ONEX entry-point wrapper for the offline-only C0 validator."""


__all__ = [
    "HandlerRsdOfflineC0",
    "ModelRsdOfflineC0Input",
    "ModelRsdOfflineC0Output",
    "NodeRsdOfflineC0ValidateCompute",
    "validate_rsd_offline_c0",
]
