# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the lab-first routing check (OMN-17427)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumLabFirstFindingClass(StrEnum):
    """The ways a routing contract can fail the lab-first rule."""

    POLICY_INVALID = "policy_invalid"
    RUBRIC_MISSING = "rubric_missing"
    RUBRIC_INCONSISTENT = "rubric_inconsistent"
    CLOUD_FIRST_TIER_ORDER = "cloud_first_tier_order"
    ENTRY_SHARE_OFF_LAB = "entry_share_off_lab"
    NON_LAB_MODEL_PIN = "non_lab_model_pin"


class ModelLabFirstRoutingRequest(BaseModel):
    """The two parsed routing contracts the rule is judged over."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class_contracts: dict[str, object] = Field(
        description="Parsed task_class_contracts.v1.yaml."
    )
    routing_tiers: dict[str, object] = Field(description="Parsed routing_tiers.yaml.")


class ModelLabFirstFinding(BaseModel):
    """One class (or the policy itself) that breaks the rule, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(
        description="The task class, or <policy> for a fault in the policy itself."
    )
    finding_class: EnumLabFirstFindingClass
    detail: str


class ModelLabFirstRoutingResult(BaseModel):
    """Every finding; ``ok`` only when there are none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    checked_classes: int = Field(ge=0)
    findings: tuple[ModelLabFirstFinding, ...] = ()


__all__ = [
    "EnumLabFirstFindingClass",
    "ModelLabFirstFinding",
    "ModelLabFirstRoutingRequest",
    "ModelLabFirstRoutingResult",
]
