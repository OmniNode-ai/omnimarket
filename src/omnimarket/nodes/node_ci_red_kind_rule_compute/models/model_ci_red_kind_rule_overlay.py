# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelCiRedKindRule(BaseModel):
    """One rule: a pattern to search in a failed check's name and the kind it names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(..., min_length=1)
    check_name_pattern: str = Field(..., min_length=1)
    ignore_case: bool

    @field_validator("check_name_pattern")
    @classmethod
    def _validate_pattern(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            msg = f"check_name_pattern {value!r} is not a compilable regular expression: {exc}"
            raise ValueError(msg) from None
        return value


class ModelCiRedKindRuleOverlay(BaseModel):
    """The overlay rule table; rules are tried in declared order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["ci_red_kind_rules.v1"]
    rules: tuple[ModelCiRedKindRule, ...] = Field(..., min_length=1)


__all__: list[str] = ["ModelCiRedKindRule", "ModelCiRedKindRuleOverlay"]
