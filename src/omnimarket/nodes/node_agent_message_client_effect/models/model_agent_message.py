# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The agent message as the client sees it, and the closed sets it is checked against."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_agent_message_kind import EnumAgentMessageKind
from omnimarket.enums.enum_agent_message_platform import EnumAgentMessagePlatform


class ModelAgentMessage(BaseModel):
    """One message on a thread, addressed to a lane or a ``platform:name``."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    message_id: str = Field(min_length=1)
    sender: str = Field(min_length=1, alias="from")
    to: str = Field(min_length=1)
    thread: str = Field(min_length=1)
    kind: EnumAgentMessageKind
    platform: EnumAgentMessagePlatform
    text: str = Field(min_length=1)
    sent_at: datetime


class ModelAgentMessageQuery(BaseModel):
    """A read of the messages addressed to one recipient."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    to: str = Field(min_length=1)
    thread: str | None = None
    since: datetime | None = None
    limit: int = Field(default=50, ge=1, le=1000)


__all__: list[str] = [
    "EnumAgentMessageKind",
    "EnumAgentMessagePlatform",
    "ModelAgentMessage",
    "ModelAgentMessageQuery",
]
