# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bash-only reduction wire models; no implicit coercion of the exit status."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelToolResultReduceRequest(BaseModel):
    """Original Bash output and its authoritative status, supplied by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tool_use_id: str = Field(min_length=1, pattern=r"^\S+$")
    exit_status: int
    output: str
    tool_name: Literal["Bash"] = "Bash"


class ModelToolResultReduceResult(BaseModel):
    """Safe result; error_lines deliberately includes all non-empty source lines."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    reduced_text: str
    exit_status: int
    error_lines: tuple[str, ...]
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_record_id: None = Field(
        default=None,
        description="No stored original in this compute-only slice; never a synthetic id.",
    )
