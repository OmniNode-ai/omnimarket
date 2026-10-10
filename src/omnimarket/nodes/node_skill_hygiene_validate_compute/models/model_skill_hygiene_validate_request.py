# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One skill-hygiene validation over a skills-tree snapshot."""

from pydantic import BaseModel, ConfigDict

from omnimarket.models.skill_tree import ModelSkillTreeSnapshot


class ModelSkillHygieneValidateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tree: ModelSkillTreeSnapshot
    # Strict promotes skill-md-required and name-matches-dir from warning to error.
    strict: bool = False
