# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Send each AUTO reply, to nobody else, and log it on the ledger.

The effect re-checks what the compute node decided: a send is refused unless the item is
AUTO, the person is on the allowlist, and the conversation is private to the operator and
allowlisted people. A refusal is a typed record, never a swallowed exception. A transport
that is not wired refuses the send (the Slack bot credential in the secret store cannot
post as the operator, so no Slack sender is built here).
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_awaiting_reply_compute.models.model_awaiting_reply import (
    EnumConversationKind,
    EnumReplyClass,
    EnumWaitSource,
    ModelWaitingItem,
)
from omnimarket.nodes.node_awaiting_reply_effect.models.model_awaiting_reply_send import (
    EnumSendOutcome,
    EnumSendRefusal,
    ModelAwaitingReplySendRequest,
    ModelAwaitingReplySendResult,
    ModelSendRecord,
)
from omnimarket.nodes.node_awaiting_reply_effect.protocols.protocol_awaiting_reply_ports import (
    ProtocolLedgerAppender,
    ProtocolLinearCommenter,
    ProtocolSlackSender,
)

LINEAR_ACTOR_LINE = "actor: awaiting-reply (sonnet)"
_TICKET_ID = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
_PRIVATE_SLACK = (EnumConversationKind.DM, EnumConversationKind.GROUP)
_RULING_REF = re.compile(r"ruling ([^;]+)")


def _cell(text: str) -> str:
    """One ledger-cell-safe line: no pipe, no newline."""
    return " ".join(text.replace("|", "/").split())


class HandlerAwaitingReplySend:
    def __init__(
        self,
        *,
        slack: ProtocolSlackSender | None = None,
        linear: ProtocolLinearCommenter | None = None,
        ledger: ProtocolLedgerAppender | None = None,
    ) -> None:
        self._slack = slack
        self._linear = linear
        self._ledger = ledger

    def handle(
        self, request: ModelAwaitingReplySendRequest
    ) -> ModelAwaitingReplySendResult:
        slack_ids = {e.slack_id for e in request.allowlist if e.slack_id}
        linear_ids = {e.linear_user_id for e in request.allowlist if e.linear_user_id}
        seen: set[tuple[EnumWaitSource, str]] = set()
        records = [
            self._one(request, item, slack_ids, linear_ids, seen)
            for item in request.sends
        ]
        return ModelAwaitingReplySendResult(
            correlation_id=request.correlation_id,
            dry_run=request.dry_run,
            records=records,
            sent_count=sum(
                r.outcome in (EnumSendOutcome.SENT, EnumSendOutcome.SENT_UNLOGGED)
                for r in records
            ),
            unlogged_count=sum(
                r.outcome is EnumSendOutcome.SENT_UNLOGGED for r in records
            ),
            refused_count=sum(
                r.outcome in (EnumSendOutcome.REFUSED, EnumSendOutcome.FAILED)
                for r in records
            ),
        )

    def _record(
        self,
        item: ModelWaitingItem,
        outcome: EnumSendOutcome,
        *,
        refusal: EnumSendRefusal | None = None,
        detail: str = "",
        text: str = "",
        permalink: str = "",
        row: str = "",
    ) -> ModelSendRecord:
        return ModelSendRecord(
            link=item.link,
            person=item.person,
            source=item.source,
            outcome=outcome,
            refusal=refusal,
            detail=detail,
            text=text,
            permalink=permalink,
            ledger_row=row,
        )

    def _refuse(
        self, item: ModelWaitingItem, why: EnumSendRefusal, detail: str = ""
    ) -> ModelSendRecord:
        return self._record(item, EnumSendOutcome.REFUSED, refusal=why, detail=detail)

    def _one(
        self,
        request: ModelAwaitingReplySendRequest,
        item: ModelWaitingItem,
        slack_ids: set[str],
        linear_ids: set[str],
        seen: set[tuple[EnumWaitSource, str]],
    ) -> ModelSendRecord:
        if item.reply_class is not EnumReplyClass.AUTO:
            return self._refuse(item, EnumSendRefusal.NOT_AUTO)
        reply = (item.drafted_reply or "").strip()
        if not reply:
            return self._refuse(item, EnumSendRefusal.NO_REPLY)
        is_slack = item.source is EnumWaitSource.SLACK
        ids = slack_ids if is_slack else linear_ids
        if item.author_id not in ids or any(m not in ids for m in item.member_ids):
            return self._refuse(item, EnumSendRefusal.NOT_ON_ALLOWLIST)
        if is_slack and item.conversation_kind not in _PRIVATE_SLACK:
            return self._refuse(item, EnumSendRefusal.SHARED_CONVERSATION)
        if (
            not is_slack
            and item.conversation_kind is not EnumConversationKind.ISSUE_COMMENT
        ):
            return self._refuse(item, EnumSendRefusal.SHARED_CONVERSATION)
        if not is_slack and not _TICKET_ID.match(item.conversation_id):
            return self._refuse(
                item, EnumSendRefusal.BAD_TICKET_ID, item.conversation_id
            )
        key = (item.source, item.conversation_id)
        if key in seen:
            return self._refuse(item, EnumSendRefusal.DUPLICATE_CONVERSATION)
        seen.add(key)

        if is_slack:
            text = f"{reply}\n\n(via {request.operator_first_name}'s agent)"
        else:
            text = f"{LINEAR_ACTOR_LINE}\n\n{reply}"
        if request.dry_run:
            return self._record(item, EnumSendOutcome.DRY_RUN, text=text)

        if is_slack and self._slack is None:
            return self._refuse(item, EnumSendRefusal.SLACK_SENDER_UNAVAILABLE)
        if not is_slack and self._linear is None:
            return self._refuse(item, EnumSendRefusal.LINEAR_COMMENTER_UNAVAILABLE)
        if self._ledger is None:
            # Never send what cannot be logged.
            return self._refuse(item, EnumSendRefusal.LEDGER_UNAVAILABLE)
        try:
            if is_slack:
                assert self._slack is not None
                permalink = self._slack.send(item.conversation_id, text)
            else:
                assert self._linear is not None
                permalink = self._linear.comment(item.conversation_id, text)
        except Exception as exc:  # the type name only; its text may carry credentials
            return self._record(
                item,
                EnumSendOutcome.FAILED,
                refusal=EnumSendRefusal.SEND_FAILED,
                detail=type(exc).__name__,
                text=text,
            )
        permalink = permalink or item.link
        match = _RULING_REF.search(item.basis)
        ruling = f"RULING {match.group(1).strip()}" if match else "FACT"
        row = _cell(
            f"SENT-ON-BEHALF ({ruling}) to {item.person} {permalink}: "
            f"{reply[:160]} (basis: {item.basis})"
        )
        try:
            self._ledger.append(row)
        except Exception as exc:
            return self._record(
                item,
                EnumSendOutcome.SENT_UNLOGGED,
                detail=type(exc).__name__,
                text=text,
                permalink=permalink,
                row=row,
            )
        return self._record(
            item, EnumSendOutcome.SENT, text=text, permalink=permalink, row=row
        )
