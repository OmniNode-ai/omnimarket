# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the routing overlay bar check (OMN-22976)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumOverlayFindingClass(StrEnum):
    """The ways the routing overlay can fail to hold a frozen bar for a class."""

    OVERLAY_INVALID = "overlay_invalid"
    BAR_MISSING = "bar_missing"
    BAR_MALFORMED = "bar_malformed"
    HASH_MALFORMED = "hash_malformed"
    HASH_MISMATCH = "hash_mismatch"
    BAR_DRIFT = "bar_drift"
    CONTRACT_BAR_MISSING = "contract_bar_missing"
    UNKNOWN_CLASS = "unknown_class"


class ModelRoutingOverlayCheckRequest(BaseModel):
    """The two parsed documents the check reads; nothing is read from disk here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(
        default="", description="Carried onto the result events."
    )
    task_class_contracts: dict[str, object] = Field(
        description="Parsed task_class_contracts.v1.yaml."
    )
    bars_overlay: dict[str, object] = Field(
        description="Parsed task_class_bars.v1.yaml, the routing overlay of frozen bars."
    )


class ModelRoutingOverlayFinding(BaseModel):
    """One class (or the overlay itself) that breaks the rule, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(
        description="The task class, or <overlay> for a fault in the overlay itself."
    )
    finding_class: EnumOverlayFindingClass
    detail: str


class ModelRoutingOverlayCheckResult(BaseModel):
    """Every finding; ``ok`` only when there are none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    checked_classes: int = Field(ge=0)
    findings: tuple[ModelRoutingOverlayFinding, ...] = ()


__all__ = [
    "EnumOverlayFindingClass",
    "ModelRoutingOverlayCheckRequest",
    "ModelRoutingOverlayCheckResult",
    "ModelRoutingOverlayFinding",
]
