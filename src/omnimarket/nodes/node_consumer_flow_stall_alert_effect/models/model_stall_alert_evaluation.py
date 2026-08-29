# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B output of one trigger's evaluation (OMN-16778, redesign).

One applied event carries a whole heartbeat window's worth of rows — 309 of
them on the ``.201`` dev lane — so one dispatch decides many keys.  The
terminal event therefore carries a *batch* verdict: one decision per key it
looked at, plus the delivery record for the ones that fired.

The delivery record is the part that matters most.  The single thing this node
exists to prevent is a failure nobody is told about, and "the alert fired but
the post did not go out" is exactly that failure wearing this node's own
uniform.  So a decided-but-undelivered alert is not swallowed and it is not
routed to a malformed-input DLQ either (it was not malformed): it is stated, by
name and with its error, on the evaluation event this node publishes on every
single dispatch.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models.model_stall_alert_decision import (
    ModelConsumerFlowStallAlertDecision,
)


class ModelStallAlertDelivery(BaseModel):
    """What happened when a confirmed alert was handed to the publish topic.

    ``published`` means *this node put the command on the bus*, and nothing
    more.  It is deliberately not called "delivered": the Slack post is
    ``node_slack_publish_effect``'s to make and its own terminal events are
    where a delivered/deduped/failed answer lives.  Conflating the two is how
    "wiring is not delivery" gets lost.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer_group: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    command_topic: str = Field(
        ...,
        min_length=1,
        description="The declared topic the Slack command was published to.",
    )
    idempotency_key: str = Field(..., min_length=1)
    published: bool = Field(
        ...,
        description="Whether the command reached the bus on this dispatch.",
    )
    error: str | None = Field(
        default=None,
        description=(
            "Why it did not, stated rather than dropped. Present exactly when "
            "published is False."
        ),
    )


class ModelConsumerFlowStallAlertEvaluation(BaseModel):
    """Everything one trigger concluded, and what it did about it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    keys_evaluated: int = Field(
        ...,
        ge=0,
        description=(
            "Distinct (consumer_group, topic) keys this trigger looked at. "
            "Zero is a real answer: an applied event that wrote no rows has "
            "nothing to evaluate."
        ),
    )
    keys_skipped: int = Field(
        ...,
        ge=0,
        description=(
            "Keys the trigger named but this dispatch did not read, because "
            "the contract's max_keys_per_trigger ceiling was reached. Reported "
            "rather than silently truncated."
        ),
    )
    windows_read: int = Field(
        ...,
        ge=0,
        description="Window rows read back from the projection for this trigger.",
    )
    decisions: tuple[ModelConsumerFlowStallAlertDecision, ...] = Field(
        ...,
        description="One decision per evaluated key, in the trigger's own order.",
    )
    deliveries: tuple[ModelStallAlertDelivery, ...] = Field(
        ...,
        description="One record per decision that asked to publish.",
    )

    @property
    def alerts_published(self) -> int:
        """How many Slack commands actually reached the bus on this dispatch."""
        return sum(1 for delivery in self.deliveries if delivery.published)

    @property
    def alerts_undelivered(self) -> int:
        """How many decided alerts did NOT reach the bus. Never silently zero."""
        return sum(1 for delivery in self.deliveries if not delivery.published)


__all__ = [
    "ModelConsumerFlowStallAlertEvaluation",
    "ModelStallAlertDelivery",
]
