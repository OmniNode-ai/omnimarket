# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire mirror of the per-topic production tally (OMN-16777).

Mirrors ``omnibase_infra.models.observability.ModelTopicProduceDelta``. See
``model_consumer_flow_delta_wire`` for why this is a mirror and when it goes
away.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelTopicProduceDeltaWire(BaseModel):
    """Envelopes a node published to one topic during one window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(..., min_length=1)
    node_id: UUID
    window_start: datetime
    window_end: datetime
    window_sequence: int = Field(..., ge=0)
    messages_produced: int = Field(default=0, ge=0)


__all__ = ["ModelTopicProduceDeltaWire"]
