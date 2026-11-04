# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelWorkClassification: one unit's work type, style and cell. Frozen, no defaults for facts a source may lack."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_classification.enum_run_style import EnumRunStyle
from omnimarket.models.work_classification.enum_work_type import EnumWorkType


class EnumDecidedBy(StrEnum):
    """Which step fixed the work type."""

    RULE = "rule"
    LAB_ROUTE = "lab_route"
    NONE = "none"


class ModelRouteDimensions(BaseModel):
    """The dimensions of one ROUTE line, in the routing contract's order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    values: tuple[tuple[str, int], ...]


class ModelRouteLine(BaseModel):
    """A parsed ROUTE line. A unit without one carries route=None, never a default band."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    band: str = Field(min_length=1)
    score: int = Field(ge=0)
    dimensions: ModelRouteDimensions
    dest: str = Field(min_length=1)
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)


class ModelWorkClassification(BaseModel):
    """The classification of one work unit. No prompt text is held."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_id: str = Field(min_length=1)
    work_type: EnumWorkType
    decided_by: EnumDecidedBy
    rule_id: str | None
    cause: str | None
    run_style: EnumRunStyle | None
    route: ModelRouteLine | None
    brief_stratum: str | None
    intent_digest: str | None
    rubric_version: str = Field(min_length=1)
    route_model_version: str | None
    route_receipt_id: str | None


__all__ = [
    "EnumDecidedBy",
    "ModelRouteDimensions",
    "ModelRouteLine",
    "ModelWorkClassification",
]
