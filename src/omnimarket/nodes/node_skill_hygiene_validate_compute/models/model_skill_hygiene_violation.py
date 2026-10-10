# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A single skill-hygiene violation."""

from omnibase_core.enums.enum_severity import EnumSeverity
from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_skill_hygiene_validate_compute.models.enum_skill_hygiene_check import (
    EnumSkillHygieneCheck,
)


class ModelSkillHygieneViolation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    check: EnumSkillHygieneCheck
    severity: EnumSeverity
    message: str

    def format_line(self) -> str:
        return f"  {self.severity.value.upper()}: [{self.check.value}] {self.path}: {self.message}"
