# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One judge verdict for one item."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_failure_class import (
    EnumAcceptanceFailureClass,
)


class ModelAcceptanceVerdict(BaseModel):
    """One judge verdict for one item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    accept: bool
    quality: int = Field(ge=0, le=3)
    failure_class: EnumAcceptanceFailureClass
    reason: str = Field(min_length=1)
