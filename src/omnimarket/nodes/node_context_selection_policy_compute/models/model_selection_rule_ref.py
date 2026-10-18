# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Rule reference for context-selection policy compute (OMN-21125 / TC.2)."""

from pydantic import BaseModel, ConfigDict, Field


class ModelSelectionRuleRef(BaseModel):
    """The profile identity plus the rule id that admitted a candidate under POLICY_SCOPE_RULE."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(
        min_length=1,
        description="Identity of the profile declaring the admitting rule.",
    )
    rule_id: str = Field(
        min_length=1,
        description="Identity of the profile-declared rule admitting the candidate.",
    )


__all__ = ["ModelSelectionRuleRef"]
