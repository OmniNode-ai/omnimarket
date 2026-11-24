# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for ``node_agent_message_effect``."""

from __future__ import annotations

from typing import Final, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_agent_message_operation import EnumAgentMessageOperation

DEFAULT_READ_LIMIT: Final[int] = 50
MAX_READ_LIMIT: Final[int] = 500


class ModelAgentMessageRequest(BaseModel):
    """One operation of the node.

    ``send`` carries the message as the raw wire mapping so the handler, not the caller's construction, answers an
    incomplete message with a typed refusal. ``read`` names the recipient and bounds the answer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumAgentMessageOperation
    message: dict[str, object] | None = Field(
        default=None,
        description="send: the message, in the wire spelling (from, to, thread, kind, text, sent_at, platform).",
    )
    to: str | None = Field(
        default=None,
        min_length=1,
        description="read: the recipient whose messages are returned.",
    )
    thread: str | None = Field(
        default=None, min_length=1, description="read: only this thread, when given."
    )
    limit: int = Field(
        default=DEFAULT_READ_LIMIT,
        ge=1,
        le=MAX_READ_LIMIT,
        description="read: at most this many messages; the newest are kept.",
    )
    since: AwareDatetime | None = Field(
        default=None, description="read: only messages sent at or after this time."
    )

    @model_validator(mode="after")
    def _operation_has_its_inputs(self) -> Self:
        if self.operation is EnumAgentMessageOperation.SEND and self.message is None:
            raise ValueError("send needs a message")
        if (
            self.operation is EnumAgentMessageOperation.READ
            and not (self.to or "").strip()
        ):
            raise ValueError("read needs a recipient (to)")
        return self


__all__: list[str] = [
    "DEFAULT_READ_LIMIT",
    "MAX_READ_LIMIT",
    "ModelAgentMessageRequest",
]
