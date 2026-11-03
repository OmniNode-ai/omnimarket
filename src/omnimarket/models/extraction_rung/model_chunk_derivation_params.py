# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared inputs of the window to chunk-size rule (OMN-21140, plan task TE.3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelChunkDerivationParams(BaseModel):
    """Read from configs/extraction_rung_windows.v1.yaml, never set in a handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_reserve_tokens: int = Field(ge=0)
    instruction_tokens: int = Field(ge=0)
    safety_factor: float = Field(gt=0, le=1)
    overlap_chars: int = Field(ge=0)
