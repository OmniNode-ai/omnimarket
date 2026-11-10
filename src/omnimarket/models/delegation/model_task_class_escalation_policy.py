# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed escalation policy a task class declares for its rung ladder."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_deployment_fact_kind import EnumDeploymentFactKind
from omnimarket.models.delegation.model_deployment_fact_marker import deployment_fact


class ModelTaskClassEscalationPolicy(BaseModel):
    """How far and in what tier order a task class escalates (OMN-20287 types it).

    ``tier_order`` is the class's own ladder, a deployment's routing choice, so
    it is marked as a deployment fact.

    OMN-20172 adds the shrink-retry caps and the unit prompt wording; they are
    declared so the deployment-fact gate does not report them as undeclared
    keys.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    max_escalations: int | None = None
    paid_from_size_band: Literal["S", "M", "L"] | None = None
    tier_order: tuple[str, ...] | None = Field(
        default=None,
        description="The closed, ordered set of routing tiers this class may use.",
        json_schema_extra=deployment_fact(EnumDeploymentFactKind.ROUTING_ORDER),
    )
    max_config_swaps: int | None = Field(
        default=None,
        ge=0,
        description=(
            "How many same-backend configuration swaps one failed attempt may "
            "be retried with before the ladder moves on (OMN-20172)."
        ),
    )
    max_split_depth: int | None = Field(
        default=None,
        ge=0,
        description=(
            "How many times a request may split, and a failed unit resplit, "
            "before the ladder stops splitting (OMN-20172)."
        ),
    )
    unit_attempts_per_unit: int | None = Field(
        default=None,
        ge=1,
        description=(
            "How many attempts one unit of a split is given before the ladder "
            "escalates it (OMN-20172)."
        ),
    )
    shrink_unit_prompt: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "How a unit of a split is stated to the model; {source} is only "
            "that unit's slice, never the whole request (OMN-20172)."
        ),
    )


__all__ = ["ModelTaskClassEscalationPolicy"]
