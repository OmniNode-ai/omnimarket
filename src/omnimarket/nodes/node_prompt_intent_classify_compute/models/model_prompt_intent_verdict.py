# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The classifier's answer for one prompt, before it becomes an event."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelPromptIntentVerdict(BaseModel):
    """Raw TF-IDF category plus the typed 8-class intent it resolves to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_category: str = Field(
        ..., description="Raw classifier category (debugging, refactoring, ...)."
    )
    intent_class: str = Field(
        ...,
        description=(
            "Typed class: REFACTOR, BUGFIX, FEATURE, ANALYSIS, CONFIGURATION, "
            "DOCUMENTATION, MIGRATION or SECURITY."
        ),
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    keywords: list[str] = Field(default_factory=list)


__all__ = ["ModelPromptIntentVerdict"]
