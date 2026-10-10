# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill-hygiene verdict: it passes exactly when no violation is an error."""

from typing import Self

from omnibase_core.enums.enum_severity import EnumSeverity
from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.nodes.node_skill_hygiene_validate_compute.models.model_skill_hygiene_violation import (
    ModelSkillHygieneViolation,
)


class ModelSkillHygieneValidateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    skills_root: str
    strict: bool
    violations: tuple[ModelSkillHygieneViolation, ...]
    error_count: int
    warning_count: int
    passed: bool

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        errors = sum(v.severity == EnumSeverity.ERROR for v in self.violations)
        warnings = sum(v.severity == EnumSeverity.WARNING for v in self.violations)
        if (
            self.error_count != errors
            or self.warning_count != warnings
            or self.passed != (errors == 0)
        ):
            raise ValueError("verdict must agree with the recorded violations")
        return self
