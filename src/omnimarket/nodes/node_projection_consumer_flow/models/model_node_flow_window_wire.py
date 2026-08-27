# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire mirror of one heartbeat window's complete flow report (OMN-16777).

Mirrors ``omnibase_infra.models.observability.ModelNodeFlowWindow``. See
``model_consumer_flow_delta_wire`` for why this is a mirror and when it goes
away.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_consumer_flow.models.model_consumer_flow_delta_wire import (
    ModelConsumerFlowDeltaWire,
)
from omnimarket.nodes.node_projection_consumer_flow.models.model_topic_produce_delta_wire import (
    ModelTopicProduceDeltaWire,
)


class ModelNodeFlowWindowWire(BaseModel):
    """The ``flow_window`` field of ``onex.evt.platform.node-heartbeat.v1``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: UUID
    window_start: datetime
    window_end: datetime
    window_sequence: int = Field(..., ge=0)
    consumer_deltas: tuple[ModelConsumerFlowDeltaWire, ...] = ()
    produce_deltas: tuple[ModelTopicProduceDeltaWire, ...] = ()


__all__ = ["ModelNodeFlowWindowWire"]
