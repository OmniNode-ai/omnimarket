# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B input for the consumer-flow derivation (OMN-16777)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_consumer_flow.models.model_node_flow_window_wire import (
    ModelNodeFlowWindowWire,
)


class ModelConsumerFlowProjectionRequest(BaseModel):
    """The heartbeat, plus the two facts the derivation cannot know by itself.

    ``extra="ignore"`` because this model is validated against the WHOLE
    ``onex.evt.platform.node-heartbeat.v1`` payload — uptime, memory, node
    type and the rest are none of this node's business, and forbidding them
    would reject every heartbeat.

    ``upstream_produced_by_topic`` and ``last_observed_sequence`` are supplied
    by the caller rather than looked up here, which is what keeps ``handle()``
    a pure function of its input: no clock, no database, no ambient state, so
    the same request always derives the same rows (AC6). The writer that owns
    the database resolves them and hands them in.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    flow_window: ModelNodeFlowWindowWire | None = Field(
        default=None,
        description="The closed throughput window, absent on a priming tick",
    )
    upstream_produced_by_topic: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Envelopes the platform published TO each topic over windows "
            "overlapping this one. A topic ABSENT from this mapping has no "
            "upstream evidence at all — which is not the same as zero, and is "
            "why a quiet externally-fed topic reports IDLE rather than STARVED."
        ),
    )
    last_observed_sequence: int | None = Field(
        default=None,
        description=(
            "The highest window_sequence already materialized for this node. A "
            "gap between it and this window means a heartbeat was lost, which "
            "materializes as UNKNOWN — never as zero traffic."
        ),
    )
    known_keys: tuple[tuple[str, str], ...] = Field(
        default=(),
        description=(
            "(consumer_group, topic) pairs already seen for this node, used to "
            "name the rows a lost window would have carried."
        ),
    )


__all__ = ["ModelConsumerFlowProjectionRequest"]
