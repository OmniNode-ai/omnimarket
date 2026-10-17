# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A saved patch and two git facts about its base."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelRescuePatchReviewRequest(BaseModel):
    """A saved patch and two git facts about its base."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    patch_text: str
    applies_to_base: bool
    already_on_base: bool = False
