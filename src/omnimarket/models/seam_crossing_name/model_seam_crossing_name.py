# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the seam crossing name compute node."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.seam_field_diff import ModelSeamModelSummary


class EnumSeamCrossingDecidedBy(StrEnum):
    RULE_R = "rule_r"
    KEY_CHECK = "key_check"


class ModelSeamCrossingNaming(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    missing_required: str = Field(min_length=1)
    undeclared_key: str = Field(min_length=1)
    clean: str = Field(min_length=1)


class ModelSeamCrossingNamingConfig(BaseModel):
    """Contract configuration resolved at the composition boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    naming: ModelSeamCrossingNaming


class ModelSeamCrossing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    crossing_id: str = Field(min_length=1)
    seam_id: str
    topic: str
    consumer: ModelSeamModelSummary
    body_keys: tuple[str, ...]


class ModelSeamCrossingNameRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    crossings: tuple[ModelSeamCrossing, ...]
    naming: ModelSeamCrossingNaming | None = None

    @field_validator("crossings")
    @classmethod
    def _no_duplicate_crossing_ids(
        cls, v: tuple[ModelSeamCrossing, ...]
    ) -> tuple[ModelSeamCrossing, ...]:
        seen: set[str] = set()
        for c in v:
            if c.crossing_id in seen:
                raise ValueError(f"duplicate crossing_id: {c.crossing_id}")
            seen.add(c.crossing_id)
        return v


class ModelSeamCrossingName(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    crossing_id: str
    seam_id: str
    topic: str
    name: str
    fields: tuple[str, ...]
    decided_by: EnumSeamCrossingDecidedBy


class ModelSeamCrossingNameVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    names: tuple[ModelSeamCrossingName, ...]
    counts: dict[str, int]
