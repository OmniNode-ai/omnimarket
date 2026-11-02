# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the bus binding reducer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.bus_binding import EnumBusBinding, EnumBusBindingState


class ModelBusBindingRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prior_state: EnumBusBindingState
    durable_healthy: bool
    pending_outbox: int = Field(default=0, ge=0)
    missing_topics: tuple[str, ...] = ()


class ModelBusBindingResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: EnumBusBindingState
    binding: EnumBusBinding
    alarm: bool
    replay_outbox: bool
