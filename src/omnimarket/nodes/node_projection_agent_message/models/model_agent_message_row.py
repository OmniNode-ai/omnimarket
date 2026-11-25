# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one row of the agent_messages table."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EnumAgentMessageRowStatus(StrEnum):
    OK = "ok"
    REFUSED = "refused"


class EnumAgentMessageRefusalReason(StrEnum):
    """Why a record has no readable message: nothing came (dropped), the bytes were not a JSON object (unreadable), or the message model refused it."""

    DROPPED = "dropped"
    UNREADABLE = "unreadable"
    REFUSED = "refused"


class ModelAgentMessageRow(BaseModel):
    """A readable message, or the counted trace of a record that was not one.

    A refused row keeps its partition and offset so a replay names the same row; the message fields are empty
    except ``text``, which holds a bounded excerpt of an unreadable record.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_partition: int = Field(ge=0)
    source_offset: int = Field(ge=0)
    status: EnumAgentMessageRowStatus
    refusal_reason: EnumAgentMessageRefusalReason | None = None
    refusal_detail: str | None = None
    from_agent: str | None = None
    to_agent: str | None = None
    thread: str | None = None
    kind: str | None = None
    platform: str | None = None
    text: str | None = None
    sent_at: AwareDatetime | None = None
    broker_at: AwareDatetime | None = None
    sender_verified: bool = Field(
        default=False,
        description="True only when a deployed sender proof vouches for the sender; no proof is deployed yet.",
    )
    projected_at: AwareDatetime | None = None
