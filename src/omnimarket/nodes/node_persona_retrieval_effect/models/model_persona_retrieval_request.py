# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for persona retrieval."""

from pydantic import BaseModel, ConfigDict, Field


class ModelPersonaRetrievalRequest(BaseModel):
    """Request to retrieve the latest persona snapshot for a user."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(..., description="User identifier to retrieve persona for")
    agent_id: str | None = Field(
        default=None,
        description="Optional agent binding filter",
    )
