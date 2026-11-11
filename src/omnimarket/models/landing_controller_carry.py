# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the old landing controller decided one tick from and did for it, carried on the tick record (OMN-23665).

During the cutover period the old landing controller keeps landing while the composed workflow runs
beside it in observe mode. The workflow's own facts (the PR watcher's state) carry none of the
controller's state: no leases, records, worker results or blocker references, no gate it holds a
pull request behind. The landing snapshot step reads the controller's facts file for its last
finished tick and its decision log, and publishes both on the tick record as this model. It lives in
the shared models package because the snapshot step writes it and the tick orchestrator reads it:
the seam join test (tests/nodes/node_pr_landing_snapshot_effect/test_join_landing_snapshot_tick.py)
fails when either side changes its shape.

The controller's decision for the tick is the decision node applied to ``facts``: the controller
calls that same node on those same facts and keeps no copy of the decision itself, only the actions
it performed for it (``logged``, from its decision log).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.landing_decision import EnumLandingActionKind, ModelLandingFacts


class ModelLandingControllerLogged(BaseModel):
    """One action row of the controller's decision log for the tick."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumLandingActionKind
    subject: str = Field(..., min_length=1)
    outcome: str = Field(..., min_length=1)


class ModelLandingControllerCarry(BaseModel):
    """The controller's facts for one finished tick, and the actions its log shows it took."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    controller_tick: int = Field(
        ..., ge=1, description="The controller's own tick number."
    )
    observed_at: datetime = Field(
        description="The moment the controller's facts describe."
    )
    facts: ModelLandingFacts = Field(
        description="The facts the controller decided that tick from, as its facts file holds them."
    )
    logged: tuple[ModelLandingControllerLogged, ...] = Field(
        default=(),
        description="The action rows of the controller's decision log for that tick.",
    )
    source: str = Field(
        ..., min_length=1, description="The declared source the carry was read from."
    )


__all__: list[str] = ["ModelLandingControllerCarry", "ModelLandingControllerLogged"]
