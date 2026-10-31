# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effect's contract-declared settings (OMN-20134)."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.receipt_gate_pin_friction import EnumPinRefusalClass


class EnumPinAdvanceMode(StrEnum):
    """dry_run plans and reports; enforce publishes the branches and opens the pull requests."""

    DRY_RUN = "dry_run"
    ENFORCE = "enforce"


class ModelRefusalClassPattern(BaseModel):
    """A refusal class and the pattern of the failure text that names it (contract data)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refusal_class: EnumPinRefusalClass
    pattern: str = Field(min_length=1)

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, value: str) -> str:
        re.compile(value)
        return value


class ModelPinAdvanceEffectConfig(BaseModel):
    """``pin_advance_effect_config`` of this node's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EnumPinAdvanceMode
    source_clone_root_env: str = Field(
        min_length=1,
        description="The environment variable naming the directory that holds the "
        "source repository's clone, in a directory of the repository's own name.",
    )
    fetch_tags: bool
    bus_lane_env: str = Field(min_length=1)
    default_bus_lane: str = Field(min_length=1)
    bus_timeout_s: float = Field(gt=0)
    refusal_classes: tuple[ModelRefusalClassPattern, ...] = Field(
        description="First match wins: the class a failed pin pull request's error text names.",
    )
    default_refusal_class: EnumPinRefusalClass
