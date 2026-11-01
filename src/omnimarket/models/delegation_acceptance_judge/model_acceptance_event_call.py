# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the delegated call itself records about one judged item, for its judged event."""

from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelAcceptanceEventCall(BaseModel):
    """The delegated call behind one item: its model key, tier, correlation id and time.

    An empty delegated_model_key or delegated_tier is accepted here so that the events
    operation can refuse it with a typed issue instead of the request failing to build.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    correlation_id: UUID
    delegated_model_key: str = ""
    delegated_tier: str = ""
    call_time: AwareDatetime
