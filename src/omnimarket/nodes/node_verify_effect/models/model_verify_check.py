# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelVerifyCheck — result of a single verification check.

Related:
    - OMN-7581: migrate node_verify_effect to omnimarket
    - OMN-7575: Build loop migration epic
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelVerifyCheck(BaseModel):
    """Result of a single verification check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., description="Check name.")
    passed: bool = Field(..., description="Whether the check passed.")
    critical: bool = Field(
        default=True,
        description="Whether failure is critical (blocks loop) or just a warning.",
    )
    message: str = Field(default="", description="Details about the check result.")


__all__: list[str] = ["ModelVerifyCheck"]
