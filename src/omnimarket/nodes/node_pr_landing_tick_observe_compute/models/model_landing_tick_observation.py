# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observe mode of the landing tick: one live tick's facts and decision, and the comparison.

The live landing controller keeps writing its decisions; the node path is proven beside it by
replaying a copy of each real tick's facts through the node and comparing the node's decision with
the one the live controller made. The two must be equal field for field.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)


class ModelLandingTickObservation(BaseModel):
    """A copy of one live tick: its facts, and the decision the live controller made from them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: ModelLandingFacts
    controller_decision: ModelLandingDecision
    fair_share: bool = Field(
        default=True,
        description="Whether the live controller shared free slots across repositories (the default).",
    )


class ModelLandingTickDifference(BaseModel):
    """One path where the node's decision differs from the live controller's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(
        description="Where the two decisions differ, as a JSON path from the decision root."
    )
    controller: str = Field(
        description="The live controller's value as JSON, or <absent>."
    )
    node: str = Field(description="The node's value as JSON, or <absent>.")


class ModelLandingTickComparison(BaseModel):
    """The verdict of one observed tick."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick: int = Field(description="The facts' tick number.")
    equal: bool = Field(description="True when the two decisions are identical.")
    controller_actions: int
    node_actions: int
    differences: tuple[ModelLandingTickDifference, ...] = ()


__all__: list[str] = [
    "ModelLandingTickComparison",
    "ModelLandingTickDifference",
    "ModelLandingTickObservation",
]
