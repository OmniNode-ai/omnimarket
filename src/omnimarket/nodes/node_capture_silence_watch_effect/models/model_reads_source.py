# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topology binding and DSN variable are contract data."""

from pydantic import BaseModel, ConfigDict, Field


class ModelReadsSource(BaseModel):
    """No ambient database identity fallback is permitted."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    binding_ref: str = Field(..., min_length=1)
    dsn_env: str = Field(..., pattern=r"^[A-Z_][A-Z0-9_]*$")
