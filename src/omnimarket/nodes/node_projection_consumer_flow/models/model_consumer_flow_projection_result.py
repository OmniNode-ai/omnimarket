# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B output of the consumer-flow derivation (OMN-16777)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_consumer_flow.models.model_consumer_flow_row import (
    ModelConsumerFlowRow,
)
from omnimarket.nodes.node_projection_consumer_flow.models.model_topic_produce_delta_wire import (
    ModelTopicProduceDeltaWire,
)


class ModelConsumerFlowProjectionResult(BaseModel):
    """Everything one heartbeat window implies, with nothing written yet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flow_rows: tuple[ModelConsumerFlowRow, ...] = Field(
        default=(),
        description="Consumer rows to upsert, verdict already derived",
    )
    produce_rows: tuple[ModelTopicProduceDeltaWire, ...] = Field(
        default=(),
        description="Per-topic production tallies to upsert",
    )
    unknown_rows: tuple[ModelConsumerFlowRow, ...] = Field(
        default=(),
        description=(
            "Rows for windows that were never observed. Separated from "
            "flow_rows because they are written INSERT-only: an UNKNOWN "
            "placeholder must never overwrite a real observation that arrived "
            "late."
        ),
    )


__all__ = ["ModelConsumerFlowProjectionResult"]
