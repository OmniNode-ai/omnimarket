# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The chunk size derived from a measured window (OMN-21140, plan task TE.3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelChunkDerivation(BaseModel):
    """max_chars is None unless a measured window produced it; the measurement id traces it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["derived", "unmeasured", "window_too_small"]
    max_chars: int | None
    overlap_chars: int
    window_measurement_id: str | None
    window_tokens: int | None
