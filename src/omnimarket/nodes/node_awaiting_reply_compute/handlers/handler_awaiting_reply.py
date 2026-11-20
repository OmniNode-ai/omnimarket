# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classification of the asks waiting on the operator (port of the awaiting-reply skill).

Pure: reads nothing, sends nothing. The session reads Slack and Linear and hands the reads
in as typed items; this handler decides what is waiting and which replies are AUTO. A
payload that does not fit the models is refused with the field named (``handle_raw``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from omnimarket.nodes.node_awaiting_reply_compute.models.model_awaiting_reply import (
    EnumConversationKind,
    EnumExclusionReason,
    EnumReadStatus,
    EnumReplyClass,
    EnumWaitSource,
    ModelAllowlistEntry,
    ModelAwaitingReplyInputItem,
    ModelAwaitingReplyRefusal,
    ModelAwaitingReplyRequest,
    ModelAwaitingReplyResult,
    ModelExcludedItem,
    ModelFieldError,
    ModelSourceRead,
    ModelSourceStatus,
    ModelWaitingItem,
)

FIRST_BOUND_HOURS = 24
SECOND_BOUND_HOURS = 72

_ACK_WORDS = frozenset(
    [
        "ok",
        "okay",
        "k",
        "sure",
        "thanks",
        "thank",
        "thx",
        "ty",
        "you",
        "got",
        "it",
        "noted",
        "great",
        "cool",
        "nice",
        "perfect",
        "sounds",
        "good",
        "will",
        "do",
        "yes",
        "yep",
        "yup",
        "np",
        "appreciated",
        "awesome",
    ]
)
_WORDS = re.compile(r"[a-z0-9']+")
# Tokens a reply may only state when its basis states them: links, ticket ids, numbers.
_FACT_TOKEN = re.compile(r"https?://\S+|[A-Z]{2,}-\d+|\d+(?:[.:/-]\d+)*")
_SHARED_CHANNELS = (EnumConversationKind.CHANNEL, EnumConversationKind.THREAD)


def is_bare_acknowledgement(text: str) -> bool:
    lowered = text.lower()
    if "?" in lowered:
        return False
    words = _WORDS.findall(lowered)
    if not words:
        return True  # an emoji or a thumbs-up alone
    return len(words) <= 6 and all(w in _ACK_WORDS for w in words)


def _tokens(text: str) -> set[str]:
    return {t.rstrip(".,;:)") for t in _FACT_TOKEN.findall(text)}


def _allowlist_ids(
    allowlist: list[ModelAllowlistEntry], source: EnumWaitSource
) -> set[str]:
    if source is EnumWaitSource.SLACK:
        return {e.slack_id for e in allowlist if e.slack_id}
    return {e.linear_user_id for e in allowlist if e.linear_user_id}


def _source_status(read: ModelSourceRead) -> ModelSourceStatus:
    if not read.ok:
        status = EnumReadStatus.FAILED
    elif read.items_read == 0 and not read.control_rows:
        # A zero without a positive control cannot be told from a failed call.
        status = EnumReadStatus.EMPTY_UNVERIFIED
    else:
        status = EnumReadStatus.OK
    return ModelSourceStatus(
        source=read.source,
        status=status,
        items_read=read.items_read,
        detail=read.detail,
    )


def _escalate_reason(
    item: ModelAwaitingReplyInputItem,
    ids: set[str],
    source_ok: bool,
) -> str | None:
    """The first section 5a test the item fails, or None when every test holds."""
    if not source_ok:
        return "source not read cleanly"
    if not ids:
        return "allowlist empty"
    if item.conversation_kind in _SHARED_CHANNELS:
        return "channel or thread: others read it"
    if item.author_id not in ids:
        return "recipient not on the allowlist"
    if any(member not in ids for member in item.member_ids):
        return "conversation holds someone not on the allowlist"
    if item.escalate_categories:
        cats = ", ".join(c.value for c in item.escalate_categories)
        return f"escalate category: {cats}"
    reply = item.proposed_reply
    if reply is None:
        return "no drafted reply"
    if not reply.basis:
        return "no basis"
    basis_text = " ".join(f"{b.ref} {b.statement}" for b in reply.basis)
    unbacked = sorted(_tokens(reply.text) - _tokens(basis_text))
    if unbacked:
        return f"reply states {', '.join(unbacked)} not in the basis"
    return None


def _wait_hours(item: ModelAwaitingReplyInputItem, now: datetime) -> float:
    return round(max((now - item.posted_at).total_seconds(), 0.0) / 3600.0, 2)


class HandlerAwaitingReply:
    """Classify items into waiting (AUTO or ESCALATE) and excluded, with read status."""

    def handle(self, request: ModelAwaitingReplyRequest) -> ModelAwaitingReplyResult:
        reads = {r.source: _source_status(r) for r in request.source_reads}
        waiting: list[ModelWaitingItem] = []
        excluded: list[ModelExcludedItem] = []
        logged = {(row.link, row.stage) for row in request.ladder_rows}

        for item in request.items:
            reason = self._exclusion(item)
            if reason is not None:
                excluded.append(
                    ModelExcludedItem(
                        link=item.link,
                        person=item.author,
                        source=item.source,
                        reason=reason,
                    )
                )
                continue
            ids = _allowlist_ids(request.allowlist, item.source)
            source_ok = reads[item.source].status is EnumReadStatus.OK
            why = _escalate_reason(item, ids, source_ok)
            hours = _wait_hours(item, request.now)
            reply = item.proposed_reply
            if why is None and reply is not None:
                basis = "; ".join(f"{b.kind.value} {b.ref}" for b in reply.basis)
                stage, to_fire = 0, []
                klass = EnumReplyClass.AUTO
            else:
                basis = why or "no basis"
                klass = EnumReplyClass.ESCALATE
                stage = (
                    2
                    if hours >= SECOND_BOUND_HOURS
                    else 1
                    if hours >= FIRST_BOUND_HOURS
                    else 0
                )
                to_fire = [
                    s for s in range(1, stage + 1) if (item.link, s) not in logged
                ]
            waiting.append(
                ModelWaitingItem(
                    person=item.author,
                    source=item.source,
                    link=item.link,
                    wait_hours=hours,
                    reply_class=klass,
                    basis=basis,
                    drafted_reply=reply.text if reply else None,
                    escalation_stage=stage,
                    stages_to_fire=to_fire,
                    reply_kind=reply.kind if reply else None,
                    conversation_id=item.conversation_id,
                    conversation_kind=item.conversation_kind,
                    author_id=item.author_id,
                    member_ids=item.member_ids,
                    subject_owner=item.subject_owner,
                )
            )

        waiting.sort(key=lambda w: w.wait_hours, reverse=True)
        return ModelAwaitingReplyResult(
            window_hours=request.window_hours,
            sources=list(reads.values()),
            waiting=waiting,
            excluded=excluded,
            held=request.held,
            auto_count=sum(w.reply_class is EnumReplyClass.AUTO for w in waiting),
            escalate_count=sum(
                w.reply_class is EnumReplyClass.ESCALATE for w in waiting
            ),
            allowlist_size=len(request.allowlist),
            allowlist=request.allowlist,
        )

    @staticmethod
    def _exclusion(item: ModelAwaitingReplyInputItem) -> EnumExclusionReason | None:
        if item.author_is_bot:
            return EnumExclusionReason.BOT
        if item.operator_replied_since:
            return EnumExclusionReason.ANSWERED_SINCE
        if is_bare_acknowledgement(item.text):
            return EnumExclusionReason.ACKNOWLEDGEMENT
        return None

    def handle_raw(
        self, payload: Mapping[str, Any]
    ) -> ModelAwaitingReplyResult | ModelAwaitingReplyRefusal:
        """Validate an untyped read bundle, then classify it; a misfit is a typed refusal."""
        try:
            request = ModelAwaitingReplyRequest.model_validate(payload)
        except ValidationError as exc:
            return ModelAwaitingReplyRefusal(
                errors=[
                    ModelFieldError(
                        field=".".join(str(p) for p in err["loc"]) or "<root>",
                        message=err["msg"],
                    )
                    for err in exc.errors()
                ]
            )
        return self.handle(request)
