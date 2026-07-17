# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delivery replay projection compute node (OMN-14726, B6).

Pure COMPUTE. Folds a self-contained, ordered delivery sequence into a
deterministic projection checksum + terminal cursor. It is the comparison tool
underpinning the B6 canary-acceptance gate: replaying the same sequence yields
an identical checksum + cursor, and a divergent sequence differs. No live
bus/DB dependency.
"""

from omnimarket.nodes.node_delivery_replay_projection_compute.handlers.handler_delivery_replay_projection import (
    HandlerDeliveryReplayProjection,
    project_delivery_sequence,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_delivery_event import (
    ModelDeliveryEvent,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_delivery_position import (
    ModelDeliveryPosition,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_delivery_replay_input import (
    ModelDeliveryReplayInput,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_replay_cursor import (
    ModelReplayCursor,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_replay_expectation import (
    ModelReplayExpectation,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_replay_projection import (
    ModelReplayProjection,
)


class NodeDeliveryReplayProjectionCompute(HandlerDeliveryReplayProjection):
    """ONEX entry-point wrapper for HandlerDeliveryReplayProjection (OMN-14726)."""


__all__ = [
    "HandlerDeliveryReplayProjection",
    "ModelDeliveryEvent",
    "ModelDeliveryPosition",
    "ModelDeliveryReplayInput",
    "ModelReplayCursor",
    "ModelReplayExpectation",
    "ModelReplayProjection",
    "NodeDeliveryReplayProjectionCompute",
    "project_delivery_sequence",
]
