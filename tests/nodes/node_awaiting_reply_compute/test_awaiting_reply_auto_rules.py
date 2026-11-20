# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every AUTO test of the awaiting-reply skill's classification, as a unit test."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_awaiting_reply_compute.models.model_awaiting_reply import (
    EnumExclusionReason,
    EnumReplyClass,
)
from tests.nodes.node_awaiting_reply_compute.test_awaiting_reply_compute import (
    BASIS,
    NOW,
    bundle,
    item,
    run,
)

pytestmark = pytest.mark.unit


def klass(payload: dict[str, Any]) -> tuple[EnumReplyClass, str]:
    w = run(payload).waiting[0]
    return w.reply_class, w.basis


def test_awaiting_reply_auto_rules_allowlisted_dm_with_basis_is_auto() -> None:
    cls, basis = klass(bundle(item()))
    assert cls is EnumReplyClass.AUTO
    assert "ruling 2026-10-16T16:30:36Z orchestrator" in basis


def test_awaiting_reply_auto_rules_channel_is_escalate() -> None:
    cls, why = klass(bundle(item(conversation_kind="channel")))
    assert cls is EnumReplyClass.ESCALATE
    assert "channel" in why


def test_awaiting_reply_auto_rules_thread_is_escalate() -> None:
    assert klass(bundle(item(conversation_kind="thread")))[0] is EnumReplyClass.ESCALATE


def test_awaiting_reply_auto_rules_recipient_off_allowlist_is_escalate() -> None:
    cls, why = klass(bundle(item(author_id="U-STRANGER")))
    assert cls is EnumReplyClass.ESCALATE
    assert "allowlist" in why


def test_awaiting_reply_auto_rules_group_with_stranger_is_escalate() -> None:
    cls, _ = klass(bundle(item(conversation_kind="group", member_ids=["U-STRANGER"])))
    assert cls is EnumReplyClass.ESCALATE
    ok = klass(bundle(item(conversation_kind="group", member_ids=["U-ONE"])))
    assert ok[0] is EnumReplyClass.AUTO


def test_awaiting_reply_auto_rules_empty_allowlist_makes_all_escalate() -> None:
    result = run(bundle(item(), allowlist=[]))
    assert {w.reply_class for w in result.waiting} == {EnumReplyClass.ESCALATE}
    assert "allowlist empty" in result.waiting[0].basis
    assert result.allowlist_size == 0


def test_awaiting_reply_auto_rules_bot_is_excluded() -> None:
    assert (
        run(bundle(item(author_is_bot=True))).excluded[0].reason
        is EnumExclusionReason.BOT
    )


def test_awaiting_reply_auto_rules_bare_acknowledgement_is_excluded() -> None:
    result = run(bundle(item(text="Sure. Thank you")))
    assert result.excluded[0].reason is EnumExclusionReason.ACKNOWLEDGEMENT
    assert result.waiting == []


def test_awaiting_reply_auto_rules_answered_since_is_excluded() -> None:
    result = run(bundle(item(operator_replied_since=True)))
    assert result.excluded[0].reason is EnumExclusionReason.ANSWERED_SINCE


def linear_item(**over: Any) -> dict[str, Any]:
    base = item(
        source="linear",
        conversation_id="OMN-1000",
        link="https://linear.example/issue/OMN-1000#c1",
        author_id="L-ONE",
        conversation_kind="issue_comment",
        text="Who owns OMN-1000?",
    )
    base["proposed_reply"] = {
        "kind": "routing",
        "text": "The orchestrator lane owns OMN-1000.",
        "basis": [
            *BASIS,
            {
                "kind": "fact",
                "ref": "linear OMN-1000",
                "statement": "OMN-1000 is owned by the orchestrator lane",
            },
        ],
    }
    base.update(over)
    return base


def test_awaiting_reply_auto_rules_linear_ask_from_allowlisted_person_with_basis_is_auto() -> (
    None
):
    result = run(bundle(linear_item()))
    w = result.waiting[0]
    assert w.reply_class is EnumReplyClass.AUTO
    assert "ruling 2026-10-16T16:30:36Z orchestrator" in w.basis


def test_awaiting_reply_auto_rules_linear_ask_without_basis_is_escalate() -> None:
    bare = linear_item()
    bare["proposed_reply"] = {
        "kind": "routing",
        "text": "Owned by the lane.",
        "basis": [],
    }
    cls, why = klass(bundle(bare))
    assert cls is EnumReplyClass.ESCALATE
    assert why == "no basis"


def test_awaiting_reply_auto_rules_reply_stating_unbacked_number_is_escalate() -> None:
    bad = item()
    bad["proposed_reply"]["text"] = "The orchestrator lane owns it; ETA 2026-12-01."
    cls, why = klass(bundle(bad))
    assert cls is EnumReplyClass.ESCALATE
    assert "2026-12-01" in why


def test_awaiting_reply_auto_rules_escalate_category_wins() -> None:
    cls, why = klass(bundle(item(escalate_categories=["money_or_date"])))
    assert cls is EnumReplyClass.ESCALATE
    assert "money_or_date" in why


def test_awaiting_reply_auto_rules_no_drafted_reply_is_escalate() -> None:
    assert klass(bundle(item(proposed_reply=None)))[1] == "no drafted reply"


def test_awaiting_reply_auto_rules_wait_hours_measured_to_now() -> None:
    assert run(bundle(item())).waiting[0].wait_hours == 3.0
    assert NOW
