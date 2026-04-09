# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pydantic import BaseModel, Field


class GitHubEvent(BaseModel):
    event_type: str = Field(..., description="Type of GitHub event")
    payload: dict = Field(..., description="Event payload data")
    delivery_id: str = Field(..., description="Unique delivery identifier")
