# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of sending the AUTO replies of an awaiting-reply run."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_awaiting_reply_compute.models.model_awaiting_reply import (
    EnumWaitSource,
    ModelAllowlistEntry,
    ModelWaitingItem,
)


class EnumSendOutcome(StrEnum):
    SENT = "sent"
    SENT_UNLOGGED = "sent_unlogged"
    DRY_RUN = "dry_run"
    REFUSED = "refused"
    FAILED = "failed"


class EnumSendRefusal(StrEnum):
    NOT_AUTO = "not_auto"
    NO_REPLY = "no_reply"
    NOT_ON_ALLOWLIST = "not_on_allowlist"
    SHARED_CONVERSATION = "shared_conversation"
    DUPLICATE_CONVERSATION = "duplicate_conversation"
    BAD_TICKET_ID = "bad_ticket_id"
    SLACK_SENDER_UNAVAILABLE = "slack_sender_unavailable"
    LINEAR_COMMENTER_UNAVAILABLE = "linear_commenter_unavailable"
    LEDGER_UNAVAILABLE = "ledger_unavailable"
    SEND_FAILED = "send_failed"


class ModelAwaitingReplySendRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    lane: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    operator_first_name: str = Field(min_length=1)
    allowlist: list[ModelAllowlistEntry]
    sends: list[ModelWaitingItem]
    dry_run: bool = Field(
        default=True, description="Sends are off unless this is false."
    )


class ModelSendRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    link: str
    person: str
    source: EnumWaitSource
    outcome: EnumSendOutcome
    refusal: EnumSendRefusal | None = None
    detail: str = ""
    text: str = Field(
        default="", description="The text sent, or that a dry run composed."
    )
    permalink: str = ""
    ledger_row: str = ""


class ModelAwaitingReplySendResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    dry_run: bool
    records: list[ModelSendRecord]
    sent_count: int
    unlogged_count: int
    refused_count: int
