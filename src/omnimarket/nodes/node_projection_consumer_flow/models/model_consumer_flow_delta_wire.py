# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire mirror of the per-consumer flow delta the runtime heartbeat carries.

OMN-16777.  The producing model is
``omnibase_infra.models.observability.ModelConsumerFlowDelta``.  This is a
field-for-field mirror rather than a direct import for one reason, stated
plainly so it is not mistaken for taste: the field ships in an omnibase-infra
version newer than the one this repo currently resolves, so importing it would
make this node unbuildable until an infra release lands.  Mirroring keeps the
two PRs independently mergeable.

The mirror is ``extra="forbid"``, not ``extra="ignore"``.  OMN-14490/OMN-14506
recorded what slim ``extra="ignore"`` copies cost the registration projection —
every field they did not declare was dropped in silence.  Forbidding extras
means a producer-side field addition breaks LOUDLY here instead of vanishing.
The producing repo carries the paired guard
(``test_flow_delta_wire_field_set_is_pinned``), so drift is caught on the side
that causes it.

FOLLOW-UP (not a silent shortcut): once an omnibase-infra release carrying
``ModelConsumerFlowDelta`` is pinned here, delete this mirror and import the
canonical model, exactly as OMN-14490 did for the heartbeat/introspection
events.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelConsumerFlowDeltaWire(BaseModel):
    """One (consumer_group, topic) throughput delta as it arrives on the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer_group: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    node_id: UUID
    window_start: datetime
    window_end: datetime
    window_sequence: int = Field(..., ge=0)
    messages_in: int = Field(default=0, ge=0)
    messages_out: int = Field(default=0, ge=0)
    messages_dlq: int = Field(default=0, ge=0)
    handler_errors: int = Field(default=0, ge=0)


__all__ = ["ModelConsumerFlowDeltaWire"]
