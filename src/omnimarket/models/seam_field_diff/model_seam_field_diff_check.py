# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the seam field diff merge check (OMN-24121).

The check reads the contracts as they stand and the base revision's source, asks the pure
diff node what is new, and answers with an exit code and the lines to print.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_field_diff.model_seam_field_diff import (
    ModelSeamFieldFinding,
)


class ModelSeamFieldDiffCheckRequest(BaseModel):
    """repo_root is the checkout whose contracts are read; base_ref the revision it is compared with."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    repo_root: str = Field(..., min_length=1)
    base_ref: str = Field(..., min_length=1)


class ModelSeamFieldDiffCheckResult(BaseModel):
    """Exit 0 clean, 1 a new difference, 2 the base or the contracts could not be read."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    exit_code: int = Field(..., ge=0, le=2)
    status: Literal["clean", "refused", "unreadable"]
    pairs_compared: int = Field(default=0, ge=0)
    adapter_skipped_count: int = Field(default=0, ge=0)
    unreadable_count: int = Field(default=0, ge=0)
    adapter_skipped: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    new_findings: tuple[ModelSeamFieldFinding, ...] = ()
    output_lines: tuple[str, ...] = ()
