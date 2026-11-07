# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build dependencies from the deployment overlay, without packaged defaults."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_lane_failure import ModelLaneFailureOverlay
from omnimarket.protocols.protocol_lane_failure import ProtocolLaneFailureGateway

from .gateway_lane_failure import LaneFailureGateway


def build_dependencies(
    environ: Mapping[str, str] | None = None,
    *,
    explicit: object | None = None,
) -> tuple[ModelLaneFailureOverlay, ProtocolLaneFailureGateway]:
    overlay = load_structured_overlay(
        "lane_failure_recovery", ModelLaneFailureOverlay, environ, explicit=explicit
    )
    return overlay, LaneFailureGateway(overlay.bindings)
