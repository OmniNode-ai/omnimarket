# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One judge call: its prompt and the item ids it must answer exactly once."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceBatch(BaseModel):
    """One judge call: its prompt and the item ids it must answer exactly once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str = Field(min_length=1)
    role: str = Field(pattern="^(primary|secondary)$")
    item_ids: tuple[str, ...] = Field(min_length=1)
    prompt: str = Field(min_length=1)
