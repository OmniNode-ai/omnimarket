# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One topic record a reader refused, with its typed reason."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_agent_message_verification_reason import (
    EnumAgentMessageVerificationReason,
)


class ModelAgentMessageRejection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: EnumAgentMessageVerificationReason
    detail: str = Field(
        default="", description="The reason in words; never the message body."
    )
    sender: str | None = Field(
        default=None, description="The declared sender, when the record had one."
    )
    message_id: str | None = None


__all__: list[str] = ["ModelAgentMessageRejection"]
