# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A refusal of a judge reply or of a scored run, naming the item when there is one."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_issue_code import (
    EnumAcceptanceIssueCode,
)


class ModelAcceptanceIssue(BaseModel):
    """A refusal of a judge reply or of a scored run, naming the item when there is one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: EnumAcceptanceIssueCode
    item_id: str = ""
    message: str = Field(min_length=1)
