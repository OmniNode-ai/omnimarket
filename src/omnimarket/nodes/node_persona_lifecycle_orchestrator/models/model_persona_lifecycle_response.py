# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Response model for persona lifecycle orchestration."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelPersonaLifecycleResponse(BaseModel):
    """Response from persona lifecycle orchestration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "error"] = Field(
        ...,
        description="Orchestration outcome",
    )
    users_processed: int = Field(
        default=0,
        ge=0,
        description="Number of users whose personas were rebuilt",
    )
    personas_created: int = Field(
        default=0,
        ge=0,
        description="Number of new persona snapshots stored",
    )
    users_skipped: int = Field(
        default=0,
        ge=0,
        description="Users skipped due to insufficient data",
    )
    error_message: str | None = Field(
        default=None,
        description="Error description when status is 'error'",
    )
