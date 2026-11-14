# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The detector's settings; supplied by the overlay, never packaged."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from omnimarket.events.ticket_contract_render import (
    ModelTicketContractRenderNormalization,
)
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus


class ModelTicketHandEditSettings(BaseModel):
    """Required overlay document; every field is a deployment decision.

    The Linear rate-limit share of the list read is unmeasured, so the interval is
    an overlay value, not a constant.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_interval_seconds: PositiveInt = Field(
        default=60, description="At most one Linear list read per interval."
    )
    initial_lookback_seconds: PositiveInt = Field(
        description="Where the cursor starts when the node has never read Linear."
    )
    max_pages: PositiveInt = Field(
        default=10,
        description="Pages of changed issues one read may take; more is detector_unreadable.",
    )
    node_actor_id: str | None = Field(
        default=None,
        description=(
            "The ticket node's own Linear user id. When set, a description revision by "
            "any other actor after the node's last write is a finding even if the content matches."
        ),
    )
    normalizations: tuple[ModelTicketContractRenderNormalization, ...] = Field(
        default=(),
        description="The rewrites Linear's markdown round trip makes, passed to the parse.",
    )
    event_bus: ModelNodeEventBus | None = None
