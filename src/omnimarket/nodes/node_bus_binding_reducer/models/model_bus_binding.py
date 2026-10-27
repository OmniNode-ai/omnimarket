# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the bus binding reducer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.bus_binding import EnumBusBinding, EnumBusBindingState


class ModelBusBindingRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prior_state: EnumBusBindingState
    durable_healthy: bool


class ModelBusBindingResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: EnumBusBindingState
    binding: EnumBusBinding
    alarm: bool
    replay_outbox: bool
