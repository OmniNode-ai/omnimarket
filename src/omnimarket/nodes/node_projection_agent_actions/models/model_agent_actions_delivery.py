# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One bus delivery to the agent-actions projection."""

from pydantic import BaseModel, ConfigDict, Field


class ModelAgentActionsDelivery(BaseModel):
    """A topic and its payload, with the source coordinates when the bus gave them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1, description="Source topic, namespaced or not.")
    payload: dict[str, object] = Field(
        description="The event as published, before any model validation."
    )
    partition: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0)
