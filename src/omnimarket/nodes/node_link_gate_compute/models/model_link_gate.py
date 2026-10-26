# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and verdict for the link gate node."""

from pydantic import BaseModel, ConfigDict


class ModelLinkGateRequest(BaseModel):
    """One host's link reading for one unit.

    ``loss_pct`` is the percent of the placing host's pings lost in this read (None: not pinged, or the ping printed no summary);
    ``prior`` the earlier reads, newest first; ``local`` marks the launching host (no link); ``bus`` a unit delivered over the
    event bus, which no ssh link carries.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    loss_pct: float | None
    prior: tuple[float, ...] = ()
    local: bool = False
    bus: bool = False


class ModelLinkGateVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    refusal: str | None
    degraded: bool
