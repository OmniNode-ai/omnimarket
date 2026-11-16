# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


class ModelCiRedKindRule(BaseModel):
    """One rule: patterns to search in a failed check's name and its log excerpt, and the kind it names.

    A rule sets ``check_name_pattern``, ``log_pattern`` or both; it matches only when every
    pattern it sets is found in its own field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(..., min_length=1)
    check_name_pattern: str | None = Field(default=None, min_length=1)
    log_pattern: str | None = Field(default=None, min_length=1)
    ignore_case: bool

    @field_validator("check_name_pattern", "log_pattern")
    @classmethod
    def _validate_pattern(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return value
        try:
            re.compile(value)
        except re.error as exc:
            msg = f"{info.field_name} {value!r} is not a compilable regular expression: {exc}"
            raise ValueError(msg) from None
        return value

    @model_validator(mode="after")
    def _require_a_pattern(self) -> ModelCiRedKindRule:
        if self.check_name_pattern is None and self.log_pattern is None:
            msg = "a rule needs check_name_pattern, log_pattern or both"
            raise ValueError(msg)
        return self


class ModelCiRedKindRuleOverlay(BaseModel):
    """The overlay rule table; rules are tried in declared order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["ci_red_kind_rules.v1"]
    rules: tuple[ModelCiRedKindRule, ...] = Field(..., min_length=1)


__all__: list[str] = ["ModelCiRedKindRule", "ModelCiRedKindRuleOverlay"]
