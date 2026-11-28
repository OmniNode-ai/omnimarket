# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bounds and limits a composed-runtime boot is judged against, as the node contract and the dev declarations give them."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelBootBounds(BaseModel):
    """Loop-hold bound, health bound and the limits the kernel container runs under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    loop_hold_bound_seconds: float = Field(
        gt=0.0, description="A callback holding the loop over this fails the check."
    )
    health_bound_seconds: float = Field(
        gt=0.0, description="The first /health answer must arrive within this."
    )
    cpus: float = Field(gt=0.0, description="CPU cap of the kernel container.")
    memory_limit: str = Field(
        min_length=1,
        description="Memory limit of the kernel container, as compose spells it.",
    )
