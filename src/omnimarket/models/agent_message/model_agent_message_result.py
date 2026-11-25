# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model shared by ``node_agent_message_effect`` and its client."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_agent_message_operation import EnumAgentMessageOperation
from omnimarket.enums.enum_agent_message_refusal import EnumAgentMessageRefusal
from omnimarket.enums.enum_agent_message_verification_reason import (
    EnumAgentMessageVerificationReason,
)
from omnimarket.models.agent_message.model_agent_message import ModelAgentMessage
from omnimarket.models.agent_message.model_agent_message_rejection import (
    ModelAgentMessageRejection,
)


class ModelAgentMessageResult(BaseModel):
    """What one operation did: taken, or refused with a typed reason (and then nothing was published)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumAgentMessageOperation
    accepted: bool = Field(..., description="True when the operation was taken.")
    published: bool = Field(
        default=False,
        description="send: False when the message only reached the durable spool (a broker outage).",
    )
    topic: str | None = None
    event_id: str | None = Field(
        default=None, description="send: the content-hash id of the event."
    )
    messages: tuple[ModelAgentMessage, ...] = Field(
        default=(), description="read: the matching messages, oldest first."
    )
    skipped: int = Field(
        default=0,
        ge=0,
        description="read: records on the topic that were not well-formed messages.",
    )
    unverified: int = Field(
        default=0,
        ge=0,
        description="read: legacy-shape messages returned without a sender proof (allowed until the cutover).",
    )
    rejections: tuple[ModelAgentMessageRejection, ...] = Field(
        default=(),
        description="read: records on the topic a reader refused, each with its typed reason.",
    )
    refusal: EnumAgentMessageRefusal | None = None
    reason: EnumAgentMessageVerificationReason | None = Field(
        default=None,
        description="send: the typed reason when the message itself was refused.",
    )
    detail: str | None = Field(
        default=None, description="The refusal's reason, in words."
    )


__all__: list[str] = ["ModelAgentMessageResult"]
