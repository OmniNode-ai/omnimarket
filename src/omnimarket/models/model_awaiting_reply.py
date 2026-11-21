# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed read inputs and typed result of the awaiting-reply check.

A read that does not fit these models is refused with the field named; it is never turned
into an empty result, because an empty result from a failed read looks like a clean inbox.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumWaitSource(StrEnum):
    SLACK = "slack"
    LINEAR = "linear"


class EnumConversationKind(StrEnum):
    DM = "dm"
    GROUP = "group"
    CHANNEL = "channel"
    THREAD = "thread"
    ISSUE_COMMENT = "issue_comment"


class EnumReplyClass(StrEnum):
    AUTO = "AUTO"
    ESCALATE = "ESCALATE"


class EnumReplyKind(StrEnum):
    """The closed set of replies an AUTO item may carry."""

    STATUS = "status"
    FACT = "fact"
    RULING_ANSWER = "ruling_answer"
    ROUTING = "routing"
    ACKNOWLEDGEMENT = "acknowledgement"
    AGREED_FOLLOWUP = "agreed_followup"


class EnumBasisKind(StrEnum):
    RULING = "ruling"
    FACT = "fact"


class EnumEscalateCategory(StrEnum):
    """Categories that make an item ESCALATE whatever else holds."""

    NEW_DECISION = "new_decision"
    MONEY_OR_DATE = "money_or_date"
    OUTSIDER = "outsider"
    PEOPLE = "people"
    SCOPE_OR_PRIORITY = "scope_or_priority"
    UNCERTAIN = "uncertain"


class EnumExclusionReason(StrEnum):
    BOT = "bot"
    ACKNOWLEDGEMENT = "acknowledgement"
    ANSWERED_SINCE = "answered_since"


class EnumReadStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    EMPTY_UNVERIFIED = "empty_unverified"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("must carry a UTC offset")
    return value


class ModelBasis(_Frozen):
    """One recorded ruling or fact a reply rests on, read in the same run."""

    kind: EnumBasisKind
    ref: str = Field(
        min_length=1, description="Ruling timestamp and lane, or the fact source."
    )
    statement: str = Field(min_length=1, description="What the source said.")


class ModelProposedReply(_Frozen):
    kind: EnumReplyKind
    text: str = Field(min_length=1)
    basis: list[ModelBasis] = Field(default_factory=list)


class ModelAwaitingReplyInputItem(_Frozen):
    """One newest-message-in-a-conversation read from Slack or Linear."""

    source: EnumWaitSource
    conversation_id: str = Field(min_length=1)
    link: str = Field(min_length=1)
    author: str = Field(min_length=1)
    author_id: str = Field(min_length=1)
    author_is_bot: bool
    text: str
    posted_at: datetime
    operator_replied_since: bool
    conversation_kind: EnumConversationKind
    member_ids: list[str] = Field(
        default_factory=list,
        description="Every participant other than the operator and the author.",
    )
    escalate_categories: list[EnumEscalateCategory] = Field(default_factory=list)
    proposed_reply: ModelProposedReply | None = None
    subject_owner: str | None = None

    _aware = field_validator("posted_at")(_require_aware)

    @model_validator(mode="after")
    def _kind_matches_source(self) -> ModelAwaitingReplyInputItem:
        linear_kind = self.conversation_kind is EnumConversationKind.ISSUE_COMMENT
        if (self.source is EnumWaitSource.LINEAR) != linear_kind:
            raise ValueError("conversation_kind issue_comment is for linear items only")
        return self


class ModelAllowlistEntry(_Frozen):
    """A person who may be answered, with the id they hold in each system."""

    person: str = Field(min_length=1)
    slack_id: str | None = Field(default=None, min_length=1)
    linear_user_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_id(self) -> ModelAllowlistEntry:
        if self.slack_id is None and self.linear_user_id is None:
            raise ValueError("an allowlist entry needs a slack_id or a linear_user_id")
        return self


class ModelSourceRead(_Frozen):
    source: EnumWaitSource
    ok: bool
    items_read: int = Field(ge=0)
    control_rows: int | None = Field(
        default=None,
        ge=0,
        description="Rows a wider re-read of the same source returned (positive control).",
    )
    detail: str = ""


class ModelLadderRow(_Frozen):
    """An ESCALATION-1 or ESCALATION-2 ledger row already written for a link."""

    link: str = Field(min_length=1)
    stage: int = Field(ge=1, le=2)


class ModelHeldReply(_Frozen):
    """A reply a previous run held back; the handoff to the next run."""

    link: str = Field(min_length=1)
    person: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ModelAwaitingReplyRequest(_Frozen):
    now: datetime
    window_hours: int = Field(default=6, ge=1, le=24 * 30)
    items: list[ModelAwaitingReplyInputItem]
    source_reads: list[ModelSourceRead]
    allowlist: list[ModelAllowlistEntry] = Field(default_factory=list)
    ladder_rows: list[ModelLadderRow] = Field(default_factory=list)
    held: list[ModelHeldReply] = Field(default_factory=list)

    _aware = field_validator("now")(_require_aware)

    @model_validator(mode="after")
    def _one_read_per_source(self) -> ModelAwaitingReplyRequest:
        seen = [r.source for r in self.source_reads]
        if len(seen) != len(set(seen)):
            raise ValueError("source_reads names a source twice")
        for item in self.items:
            if item.source not in seen:
                raise ValueError(
                    f"item {item.link} comes from {item.source.value}, which has no source_reads entry"
                )
        return self


class ModelWaitingItem(_Frozen):
    person: str
    source: EnumWaitSource
    link: str
    wait_hours: float
    reply_class: EnumReplyClass
    basis: str
    drafted_reply: str | None
    escalation_stage: int = Field(ge=0, le=2)
    stages_to_fire: list[int] = Field(default_factory=list)
    reply_kind: EnumReplyKind | None = None
    conversation_id: str
    conversation_kind: EnumConversationKind
    author_id: str
    member_ids: list[str] = Field(default_factory=list)
    subject_owner: str | None = None


class ModelExcludedItem(_Frozen):
    link: str
    person: str
    source: EnumWaitSource
    reason: EnumExclusionReason


class ModelSourceStatus(_Frozen):
    source: EnumWaitSource
    status: EnumReadStatus
    items_read: int
    detail: str = ""


class ModelAwaitingReplyResult(_Frozen):
    status: str = "decided"
    window_hours: int
    sources: list[ModelSourceStatus]
    waiting: list[ModelWaitingItem]
    excluded: list[ModelExcludedItem]
    held: list[ModelHeldReply]
    auto_count: int
    escalate_count: int
    allowlist_size: int
    allowlist: list[ModelAllowlistEntry] = Field(default_factory=list)


class ModelFieldError(_Frozen):
    field: str
    message: str


class ModelAwaitingReplyRefusal(_Frozen):
    """A malformed read: the fields that did not fit, never an empty result."""

    status: str = "refused"
    errors: list[ModelFieldError] = Field(min_length=1)
