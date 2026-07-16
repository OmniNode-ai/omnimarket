# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccAutoauthorWindowRequest — the N-window counter input (OMN-14393).

The read-only observation trail the window aggregator counts over. Pure input:
a tuple of observation records + the operator-set streak threshold N. Zero I/O.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation


class ModelOccAutoauthorWindowRequest(BaseModel):
    """Input to the OCC auto-authoring window counter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: tuple[ModelOccAutoauthorObservation, ...] = Field(
        default=(),
        description="The durable observation trail (any order — sorted deterministically by the counter).",
    )
    required_streak: int = Field(
        default=10,
        ge=1,
        description="N: consecutive clean machine-minted passes required to declare flip_ready (design §4, default 10, operator-adjustable).",
    )


__all__ = ["ModelOccAutoauthorWindowRequest"]
