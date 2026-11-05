# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind deployment data and the existing bus gateway; there is no default repository."""

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_lane_routing import ModelLaneRoutingOverlay
from omnimarket.protocols.protocol_lane_routing import ProtocolLaneRoutingGateway

from ..adapters.adapter_routing_gateway import AdapterLaneRoutingGateway
from ..models.model_routing_bindings import ModelLaneRoutingDeployment


def build_dependencies() -> tuple[ModelLaneRoutingOverlay, ProtocolLaneRoutingGateway]:
    deployment = load_structured_overlay(
        "node_lane_block_diagnose_effect", ModelLaneRoutingDeployment
    )
    return deployment, AdapterLaneRoutingGateway(deployment.bindings)
