# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topology binding and DSN variable are contract data."""

from pydantic import BaseModel, ConfigDict, Field


class ModelReadsSource(BaseModel):
    """No ambient database identity fallback is permitted."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    binding_ref: str = Field(..., min_length=1)
    dsn_env: str = Field(..., pattern=r"^[A-Z_][A-Z0-9_]*$")
    # the NAME of the variable the host installer sets to the password's secret store reference
    password_ref_env: str | None = Field(default=None, pattern=r"^[A-Z_][A-Z0-9_]*$")
