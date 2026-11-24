# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One agent message on the cross-platform agent message topic."""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from omnimarket.enums.enum_agent_message_kind import EnumAgentMessageKind
from omnimarket.enums.enum_agent_message_platform import EnumAgentMessagePlatform

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

    Every field is required. The wire form spells the sender ``from`` (a Python keyword), so the
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


__all__: list[str] = ["MAX_NAME_CHARS", "MAX_TEXT_CHARS", "ModelAgentMessage"]
