# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared request for the tick fill and the pure ruling mechanism owed compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

STAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class ModelRulingMechanismOwedRequest(BaseModel):
    """Raw ledger rows (``<ts> | <TYPE> | key=value | ...``) and the tick that judges them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[str, ...]
    tick_at: str = Field(pattern=STAMP_PATTERN)
    tick_interval_seconds: int = Field(default=1800, gt=0)
