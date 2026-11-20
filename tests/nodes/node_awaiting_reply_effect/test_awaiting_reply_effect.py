# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Awaiting-reply effect: sends AUTO replies to allowlisted people and logs each one."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

import omnimarket.nodes.node_awaiting_reply_effect as node_package
from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)
from omnimarket.nodes.node_awaiting_reply_compute.handlers.handler_awaiting_reply import (
    HandlerAwaitingReply,
)
from omnimarket.nodes.node_awaiting_reply_compute.models.model_awaiting_reply import (
    EnumReplyClass,
    ModelAwaitingReplyResult,
    ModelWaitingItem,
)
from omnimarket.nodes.node_awaiting_reply_effect.handlers.handler_awaiting_reply_send import (
    LINEAR_ACTOR_LINE,
    HandlerAwaitingReplySend,
)
from omnimarket.nodes.node_awaiting_reply_effect.handlers.handler_linear_comment_port import (
    LinearCommentPort,
)
from omnimarket.nodes.node_awaiting_reply_effect.models.model_awaiting_reply_send import (
    EnumSendOutcome,
    EnumSendRefusal,
    ModelAwaitingReplySendRequest,
)
from tests.nodes.node_awaiting_reply_compute.test_awaiting_reply_auto_rules import (
    linear_item,
)
from tests.nodes.node_awaiting_reply_compute.test_awaiting_reply_compute import (
    ALLOW,
    bundle,
    item,
)

pytestmark = pytest.mark.unit
NODE_DIR = Path(node_package.__file__).parent


class FakeSlack:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def send(self, conversation_id: str, text: str) -> str:
        if self.fail:
            raise ConnectionError("token xoxp-secret rejected")
        self.sent.append((conversation_id, text))
        return f"https://slack.example/{conversation_id}/sent"


class FakeLinear:
    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []

    def comment(self, ticket_id: str, body: str) -> str:
        self.comments.append((ticket_id, body))
        return "comment-1"


class FakeLedger:
    def __init__(self, fail: bool = False) -> None:
        self.rows: list[str] = []
        self.fail = fail

    def append(self, row: str) -> None:
        if self.fail:
            raise OSError("ledger down")
        self.rows.append(row)


def waiting(*items: dict[str, Any], **kw: Any) -> list[ModelWaitingItem]:
    result = HandlerAwaitingReply().handle_raw(bundle(*items, **kw))
    assert isinstance(result, ModelAwaitingReplyResult)
    return result.waiting


def request(
    sends: list[ModelWaitingItem], dry_run: bool = False
) -> ModelAwaitingReplySendRequest:
    from omnimarket.nodes.node_awaiting_reply_compute.models.model_awaiting_reply import (
        ModelAllowlistEntry,
    )

    return ModelAwaitingReplySendRequest(
        correlation_id=uuid4(),
        lane="awaitnode-1620",
        operator_first_name="Pat",
        allowlist=[ModelAllowlistEntry.model_validate(e) for e in ALLOW],
        sends=sends,
        dry_run=dry_run,
    )


def test_awaiting_reply_effect_contract_binds_handler_and_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    binding = contract["handler"]
    handler = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeAwaitingReplyEffect, handler)
    assert contract["node_type"] == "effect"
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]


def test_awaiting_reply_effect_slack_send_has_footer_and_ledger_row() -> None:
    slack, ledger = FakeSlack(), FakeLedger()
    result = HandlerAwaitingReplySend(slack=slack, ledger=ledger).handle(
        request(waiting(item()))
    )
    [rec] = result.records
    assert rec.outcome is EnumSendOutcome.SENT
    sent_text = slack.sent[0][1]
    assert sent_text.endswith("(via Pat's agent)")
    assert slack.sent[0][0] == "D1"
    [row] = ledger.rows
    assert row.startswith(
        "SENT-ON-BEHALF (RULING 2026-10-16T16:30:36Z orchestrator) to Owner One "
    )
    assert "|" not in row
    assert "\n" not in row
    assert result.sent_count == 1


def test_awaiting_reply_effect_linear_comment_first_line_is_actor_line() -> None:
    linear, ledger = FakeLinear(), FakeLedger()
    result = HandlerAwaitingReplySend(linear=linear, ledger=ledger).handle(
        request(waiting(linear_item()))
    )
    assert result.records[0].outcome is EnumSendOutcome.SENT
    ticket, body = linear.comments[0]
    assert ticket == "OMN-1000"
    assert body.splitlines()[0] == LINEAR_ACTOR_LINE == "actor: awaiting-reply (sonnet)"
    assert len(ledger.rows) == 1


def test_awaiting_reply_effect_dry_run_sends_and_logs_nothing() -> None:
    slack, ledger = FakeSlack(), FakeLedger()
    result = HandlerAwaitingReplySend(slack=slack, ledger=ledger).handle(
        request(waiting(item()), dry_run=True)
    )
    assert result.records[0].outcome is EnumSendOutcome.DRY_RUN
    assert result.records[0].text.endswith("(via Pat's agent)")
    assert slack.sent == []
    assert ledger.rows == []
    assert result.sent_count == 0


def test_awaiting_reply_effect_sends_to_nobody_else() -> None:
    stranger = waiting(item(author_id="U-STRANGER"))[0]
    forged = stranger.model_copy(
        update={"reply_class": EnumReplyClass.AUTO, "drafted_reply": "hi"}
    )
    channel = waiting(item(conversation_kind="channel"))[0].model_copy(
        update={"reply_class": EnumReplyClass.AUTO}
    )
    slack, ledger = FakeSlack(), FakeLedger()
    result = HandlerAwaitingReplySend(slack=slack, ledger=ledger).handle(
        request([stranger, forged, channel])
    )
    assert [r.refusal for r in result.records] == [
        EnumSendRefusal.NOT_AUTO,
        EnumSendRefusal.NOT_ON_ALLOWLIST,
        EnumSendRefusal.SHARED_CONVERSATION,
    ]
    assert slack.sent == []
    assert ledger.rows == []


def test_awaiting_reply_effect_one_send_per_conversation() -> None:
    first = waiting(item())[0]
    second = first.model_copy(update={"link": "https://slack.example/D1/p2"})
    slack = FakeSlack()
    result = HandlerAwaitingReplySend(slack=slack, ledger=FakeLedger()).handle(
        request([first, second])
    )
    assert result.records[1].refusal is EnumSendRefusal.DUPLICATE_CONVERSATION
    assert len(slack.sent) == 1


def test_awaiting_reply_effect_no_slack_sender_is_a_typed_refusal() -> None:
    ledger = FakeLedger()
    result = HandlerAwaitingReplySend(ledger=ledger).handle(request(waiting(item())))
    assert result.records[0].refusal is EnumSendRefusal.SLACK_SENDER_UNAVAILABLE
    assert result.refused_count == 1
    assert ledger.rows == []


def test_awaiting_reply_effect_failed_send_is_typed_and_logs_nothing() -> None:
    ledger = FakeLedger()
    result = HandlerAwaitingReplySend(slack=FakeSlack(fail=True), ledger=ledger).handle(
        request(waiting(item()))
    )
    rec = result.records[0]
    assert rec.outcome is EnumSendOutcome.FAILED
    assert rec.detail == "ConnectionError"
    assert "xoxp" not in rec.model_dump_json()
    assert ledger.rows == []


def test_awaiting_reply_effect_ledger_failure_after_send_is_sent_unlogged() -> None:
    result = HandlerAwaitingReplySend(
        slack=FakeSlack(), ledger=FakeLedger(fail=True)
    ).handle(request(waiting(item())))
    assert result.records[0].outcome is EnumSendOutcome.SENT_UNLOGGED
    assert result.unlogged_count == 1
    assert result.sent_count == 1


def test_awaiting_reply_effect_no_ledger_means_no_send() -> None:
    slack = FakeSlack()
    result = HandlerAwaitingReplySend(slack=slack).handle(request(waiting(item())))
    assert result.records[0].refusal is EnumSendRefusal.LEDGER_UNAVAILABLE
    assert slack.sent == []


def test_awaiting_reply_effect_linear_port_posts_a_comment_operation() -> None:
    seen: list[ModelCreateTicketRequest] = []

    class Gateway:
        def handle(self, req: ModelCreateTicketRequest) -> ModelCreateTicketResult:
            seen.append(req)
            return ModelCreateTicketResult(
                status="commented",
                operation=EnumTicketOperation.COMMENT,
                comment_id="c-9",
            )

    port = LinearCommentPort("awaitnode-1620", gateway_factory=lambda _lane: Gateway())
    assert port.comment("OMN-1000", "actor: awaiting-reply (sonnet)\n\nhi") == "c-9"
    assert seen[0].operation is EnumTicketOperation.COMMENT
    assert seen[0].ticket_id == "OMN-1000"

    class Refusing:
        def handle(self, req: ModelCreateTicketRequest) -> ModelCreateTicketResult:
            return ModelCreateTicketResult(
                status="error",
                operation=EnumTicketOperation.COMMENT,
                finding_codes=["x"],
            )

    with pytest.raises(RuntimeError):
        LinearCommentPort("l", gateway_factory=lambda _lane: Refusing()).comment(
            "OMN-1000", "b"
        )
