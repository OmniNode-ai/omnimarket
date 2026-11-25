# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One agent message on the cross-platform agent message topic."""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from omnimarket.enums.enum_agent_message_kind import EnumAgentMessageKind
from omnimarket.enums.enum_agent_message_platform import EnumAgentMessagePlatform
from omnimarket.models.agent_message.model_agent_sender_proof import (
    ModelAgentSenderProof,
)

MAX_TEXT_CHARS: Final[int] = 8000
MAX_NAME_CHARS: Final[int] = 200

_Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME_CHARS),
]
_Text = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TEXT_CHARS),
]


class ModelAgentMessage(BaseModel):
    """A message from one agent to one recipient in one thread.

    Every field of the legacy shape is required. The sender proof and its companions (``message_id`` stable across
    retries, ``message_schema_version``, ``sender_proof``) are optional so the legacy wire shape stays readable until every
    reader is cut over; a reader whose cutover flag is off refuses a message with no proof. The wire form spells the sender ``from`` (a Python keyword), so the
    attribute is ``sender`` with the alias ``from``; dump with ``by_alias=True`` to get the wire form.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=False)

    sender: _Name = Field(..., alias="from", description="The sending agent or lane.")
    to: _Name = Field(..., description="The recipient agent or lane.")
    thread: _Name = Field(..., description="The conversation the message belongs to.")
    kind: EnumAgentMessageKind
    text: _Text
    sent_at: AwareDatetime = Field(
        ..., description="When the sender sent it; carries a UTC offset."
    )
    platform: EnumAgentMessagePlatform
    message_id: _Name | None = Field(
        default=None,
        description="Chosen once by the sender and kept by every retry; the event id derives from it alone.",
    )
    message_schema_version: int | None = Field(
        default=None, ge=1, description="The version of this message contract."
    )
    sender_proof: ModelAgentSenderProof | None = Field(
        default=None,
        description="The signature of the sender's registered key over the typed fields.",
    )


__all__: list[str] = ["MAX_NAME_CHARS", "MAX_TEXT_CHARS", "ModelAgentMessage"]
