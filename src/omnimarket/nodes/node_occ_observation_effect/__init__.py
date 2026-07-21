# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OCC observation append write-EFFECT node (OMN-14888)."""

from omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect import (
    HandlerOccObservationEffect,
)
from omnimarket.nodes.node_occ_observation_effect.models.model_occ_observation_effect_request import (
    ModelOccObservationEffectRequest,
)


class NodeOccObservationEffect(HandlerOccObservationEffect):
    """ONEX entry-point wrapper for HandlerOccObservationEffect (OMN-14888)."""


__all__ = [
    "HandlerOccObservationEffect",
    "ModelOccObservationEffectRequest",
    "NodeOccObservationEffect",
]
