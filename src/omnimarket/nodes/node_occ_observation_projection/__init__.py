# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OCC observation dedup projection node (OMN-14851, storage-agnostic scaffold)."""

from omnimarket.nodes.node_occ_observation_projection.handlers.handler_occ_observation_projection import (
    HandlerOccObservationProjection,
    compute_observation_projection,
)
from omnimarket.nodes.node_occ_observation_projection.models.model_occ_observation_projection_request import (
    ModelOccObservationProjectionRequest,
)
from omnimarket.nodes.node_occ_observation_projection.models.model_occ_observation_projection_result import (
    ModelOccObservationProjectionResult,
)


class NodeOccObservationProjection(HandlerOccObservationProjection):
    """ONEX entry-point wrapper for HandlerOccObservationProjection (OMN-14851)."""


__all__ = [
    "HandlerOccObservationProjection",
    "ModelOccObservationProjectionRequest",
    "ModelOccObservationProjectionResult",
    "NodeOccObservationProjection",
    "compute_observation_projection",
]
