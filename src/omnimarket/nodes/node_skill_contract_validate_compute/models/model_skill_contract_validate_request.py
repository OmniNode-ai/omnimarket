# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One skill-contract validation over a skills-tree snapshot."""

from pydantic import BaseModel, ConfigDict

from omnimarket.models.skill_tree import ModelSkillTreeSnapshot


class ModelSkillContractValidateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tree: ModelSkillTreeSnapshot
    # Strict promotes spec-prompt-predicates from warning to error.
    strict: bool = False
