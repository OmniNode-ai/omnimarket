# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay document; the interval and the identity are deployment decisions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt


class ModelTicketHandEditNormalization(BaseModel):
    """One regular-expression rewrite the tracker's own markdown round trip makes."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    pattern: str = Field(min_length=1)
    replacement: str = ""


class ModelTicketHandEditSettings(BaseModel):
    """Supplied by the overlay, never packaged."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    check_interval_seconds: PositiveInt = Field(
        default=60,
        description="At most one Linear read per this many seconds; the share of Linear's rate limit it uses is unmeasured.",
    )
    node_actor_id: str | None = Field(
        default=None,
        min_length=1,
        description="Linear user id the ticket node writes as; a revision by any other actor after the node's last write is a finding.",
    )
    initial_lookback_seconds: PositiveInt = Field(
        default=600,
        description="With no cursor yet, read issues updated within this many seconds.",
    )
    write_grace_seconds: NonNegativeInt = Field(
        default=30,
        description="An issue changed within this many seconds is judged by a later sweep, so a write's event can arrive first.",
    )
    normalizations: list[ModelTicketHandEditNormalization] = Field(
        default_factory=list,
        description="Rewrites applied to both texts before the rendered-text comparison.",
    )
