# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed bus binding state and decision (OMN-20275)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumBusBinding(StrEnum):
    DURABLE = "durable"
    INMEMORY = "inmemory"


class EnumBusBindingState(StrEnum):
    """The states of the binding FSM declared in node_bus_binding_reducer/contract.yaml."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RECOVERING = "recovering"


class ModelBusBindingOrder(BaseModel):
    """The lane's ordered fallback as the overlay declares it: durable first, then these."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    fallback: tuple[EnumBusBinding, ...] = Field(min_length=1)


class ModelBusBindingDecision(BaseModel):
    """Which binding one command used, the state it left the FSM in, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    binding: EnumBusBinding
    state: EnumBusBindingState
    prior_state: EnumBusBindingState
    reason: str
    observed_at: datetime
    pending_outbox: int = 0

    @property
    def degraded(self) -> bool:
        """True when the durable bus failed its health read and this command fell back."""
        return self.state is EnumBusBindingState.DEGRADED

    def tick_line(self) -> str:
        """The one line a tick prints naming the binding in use."""
        return (
            f"bus-binding lane={self.lane} binding={self.binding.value} "
            f"state={self.state.value} pending_outbox={self.pending_outbox}"
            + (f" reason={self.reason}" if self.reason else "")
        )

    def status_cell(self) -> str:
        """The cell a STATUS row carries so the fallback is visible on the ledger."""
        return f"bus_binding={self.binding.value} bus_state={self.state.value}"
