# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One fixed checklist item and the scorer's yes or no (OMN-19787)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelChecklistItemScore(BaseModel):
    """``met`` is None when the scorer gave no answer for the item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    met: bool | None
