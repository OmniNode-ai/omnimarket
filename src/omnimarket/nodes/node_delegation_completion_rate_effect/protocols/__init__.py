# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_delegation_completion_rate_effect: the delegation_events reader."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_delegation_completion_rate import (
    ModelDelegationEventFact,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.models.model_paid_rung_burn_run import (
    ModelPaidRungCallsRead,
)


class ModelEventsRead(BaseModel):
    """The rows of the window and the time they were read at (the source's own clock), or why they were not read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime | None = None
    events: tuple[ModelDelegationEventFact, ...] = ()
    error: str | None = None


class ProtocolDelegationEventSource(Protocol):
    def read(self, window_seconds: int) -> ModelEventsRead:
        """The rows of the last ``window_seconds``; never raises, a failure is ``error``."""
        raise NotImplementedError


class ProtocolPaidRungCallSource(Protocol):
    def read(self, since: datetime, slugs: tuple[str, ...]) -> ModelPaidRungCallsRead:
        """The completed calls on ``slugs`` at or after ``since``; never raises, a failure is ``error``."""
        raise NotImplementedError


__all__ = [
    "ModelEventsRead",
    "ProtocolDelegationEventSource",
    "ProtocolPaidRungCallSource",
]
