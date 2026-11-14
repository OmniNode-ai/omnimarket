# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCiRedKindRuleRequest(BaseModel):
    """One failed check's name to classify against the overlay's rule table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_name: str = Field(..., min_length=1)


__all__: list[str] = ["ModelCiRedKindRuleRequest"]
