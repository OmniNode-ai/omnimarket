# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection's contract-declared settings (OMN-20134)."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.receipt_gate_pin_friction import EnumPinRefusalClass


class ModelCheckRefusalPattern(BaseModel):
    """A red check whose name matches ``pattern`` is a refusal of ``refusal_class``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refusal_class: EnumPinRefusalClass
    pattern: str = Field(min_length=1)

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, value: str) -> str:
        re.compile(value)
        return value


class ModelPinFrictionConfig(BaseModel):
    """``pin_friction_config`` of this node's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    red_conclusions: frozenset[str] = Field(min_length=1)
    check_refusal_classes: tuple[ModelCheckRefusalPattern, ...]
    conflicting_mergeable: str = Field(min_length=1)
    closed_triage_states: frozenset[str] = Field(min_length=1)


__all__ = [
    "ModelCheckRefusalPattern",
    "ModelPinFrictionConfig",
]
