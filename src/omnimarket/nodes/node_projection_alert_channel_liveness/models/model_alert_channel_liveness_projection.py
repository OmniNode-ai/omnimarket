# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result models for the liveness-verdict fold."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_alert_channel_liveness.models.model_alert_channel_liveness_row import (
    ModelAlertChannelLivenessRow,
)
from omnimarket.nodes.node_projection_alert_channel_liveness.models.model_alert_channel_liveness_wire import (
    ModelAlertChannelLivenessResultWire,
)


class ModelAlertChannelLivenessProjectionRequest(ModelAlertChannelLivenessResultWire):
    """The flat domain payload the definition-B runtime adapter supplies.

    This inherits the tolerant wire model rather than wrapping it in an event
    field. The adapter constructs the input from the unwrapped payload; an
    event envelope in compute would reject that real delivery path while
    passing a writer test that happened to construct the wrapper by hand.

    Delivery facts are optional here. The writer supplies the deterministic
    coordinate key; the bare adapter path supplies neither and the fold uses
    the verdict's content. Both paths therefore converge on redelivery.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    fallback_correlation_id: str = Field(
        default="",
        description="Delivery UUID derived from topic, partition and offset.",
    )
    source_topic: str = Field(
        default="", description="Topic this result was read from."
    )

    def result(self) -> ModelAlertChannelLivenessResultWire:
        """The domain result alone, without delivery facts."""
        return ModelAlertChannelLivenessResultWire.model_validate(
            self.model_dump(
                include=set(ModelAlertChannelLivenessResultWire.model_fields)
            )
        )


class ModelAlertChannelLivenessProjectionResult(BaseModel):
    """What the fold produced: a measured row or an explicit throttled skip."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelAlertChannelLivenessRow | None = Field(
        default=None, description="The measured verdict; absent for a throttled tick."
    )
    skipped: bool = Field(default=False, description="True when probed=false.")


__all__ = [
    "ModelAlertChannelLivenessProjectionRequest",
    "ModelAlertChannelLivenessProjectionResult",
]
