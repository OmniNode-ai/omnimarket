# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Response model for persona storage operations."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelPersonaStorageResponse(BaseModel):
    """Response from persona storage operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "duplicate", "error"] = Field(
        ...,
        description="Operation outcome",
    )
    is_new_insert: bool = Field(
        default=False,
        description="True if a new row was inserted (vs duplicate skip)",
    )
    error_message: str | None = Field(
        default=None,
        description="Error description when status is 'error'",
    )
