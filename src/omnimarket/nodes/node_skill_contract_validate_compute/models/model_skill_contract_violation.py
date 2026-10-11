# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A single skill-contract violation."""

from omnibase_core.enums.enum_severity import EnumSeverity
from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_skill_contract_validate_compute.models.enum_skill_contract_check import (
    EnumSkillContractCheck,
)


class ModelSkillContractViolation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    check: EnumSkillContractCheck
    severity: EnumSeverity
    message: str

    def format_line(self) -> str:
        return f"  {self.severity.value.upper()}: [{self.check.value}] {self.path}: {self.message}"
