# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One landing tick's filled facts, as recorded on the bus (OMN-23409).

The landing snapshot step publishes it on its overlay's record_topic and the landing tick
orchestrator reads it from its overlay's snapshot_topic. It lives in the shared models package
because both nodes read it: the seam join test (tests/nodes/node_pr_landing_snapshot_effect/test_join_landing_snapshot_tick.py)
fails when either side changes its shape.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.landing_controller_carry import ModelLandingControllerCarry
from omnimarket.models.landing_decision import ModelLandingFacts


class ModelLandingTickRecord(BaseModel):
    """One tick's filled facts, as recorded on the bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    tick: int = Field(..., ge=1)
    facts: ModelLandingFacts
    provenance: str = Field(
        ..., min_length=1, description="The declared sources the facts came from."
    )
    controller: ModelLandingControllerCarry | None = Field(
        default=None,
        description=(
            "The old controller's finished tick this record's state was carried from, with its "
            "facts and logged actions; None outside the cutover period (OMN-23665)."
        ),
    )


__all__: list[str] = ["ModelLandingTickRecord"]
