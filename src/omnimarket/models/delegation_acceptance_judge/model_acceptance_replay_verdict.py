# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One judge's verdict as a committed judgments.jsonl line records it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_failure_class import (
    EnumAcceptanceFailureClass,
)


class ModelAcceptanceReplayVerdict(BaseModel):
    """The accept, quality and failure class of one judge for one committed item."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    accept: bool = Field(strict=True)
    quality: int = Field(strict=True, ge=0, le=3)
    failure_class: EnumAcceptanceFailureClass
