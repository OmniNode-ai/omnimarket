# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Share of merges that had a landing receipt, per merge source."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelMergeOverrideReceiptShare(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    per_source: dict[str, tuple[int, int]] = Field(
        description="source -> (merges, merges with a receipt)"
    )
    total: int
    with_receipt: int
