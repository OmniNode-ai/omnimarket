# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settings and result of the bounded entrypoint detail capture."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDetailCaptureSettings(BaseModel):
    """The bound and the kept head and tail, all read from the contract's ``detail_capture``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_bytes: int = Field(ge=1)
    head_lines: int = Field(ge=1)
    tail_lines: int = Field(ge=1)


class ModelDetailCaptureResult(BaseModel):
    """What one capture wrote: its line count, whether it was cut, and how many lines were dropped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_lines: int
    dropped_lines: int
    truncated: bool
