# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract-declared supply aggregate."""

from pydantic import BaseModel, ConfigDict, Field


class ModelFreshnessSupply(BaseModel):
    """Identifiers are validated before any SQL interpolation."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    relation: str = Field(..., pattern=r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")
    timestamp_column: str = Field(
        ..., pattern=r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$"
    )
