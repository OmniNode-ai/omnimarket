# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Awaiting-reply compute node: typed reads in, typed AUTO/ESCALATE decisions out."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_awaiting_reply_compute as node_package
from omnimarket.models.model_awaiting_reply import (
    EnumExclusionReason,
    EnumReadStatus,
    EnumReplyClass,
    ModelAwaitingReplyRefusal,
    ModelAwaitingReplyResult,
)
from omnimarket.nodes.node_awaiting_reply_compute.handlers.handler_awaiting_reply import (
    HandlerAwaitingReply,
    is_bare_acknowledgement,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 20, 16, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent

ALLOW = [{"person": "Owner One", "slack_id": "U-ONE", "linear_user_id": "L-ONE"}]
BASIS = [
    {
        "kind": "ruling",
        "ref": "2026-10-16T16:30:36Z orchestrator",
        "statement": "orchestrator answers team messages from recorded rulings",
    }
]


def item(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "slack",
        "conversation_id": "D1",
        "link": "https://slack.example/D1/p1",
        "author": "Owner One",
        "author_id": "U-ONE",
        "author_is_bot": False,
        "text": "Which lane owns the dashboard work?",
        "posted_at": (NOW - timedelta(hours=3)).isoformat(),
        "operator_replied_since": False,
        "conversation_kind": "dm",
        "proposed_reply": {
            "kind": "routing",
            "text": "The orchestrator lane owns it.",
            "basis": BASIS,
        },
    }
    base.update(over)
    return base


def bundle(
    *items: dict[str, Any], allowlist: Any = ALLOW, **over: Any
) -> dict[str, Any]:
    sources = {i.get("source", "slack") for i in items} or {"slack"}
    out: dict[str, Any] = {
        "now": NOW.isoformat(),
        "items": list(items),
        "source_reads": [
            {"source": s, "ok": True, "items_read": 1, "control_rows": 5}
            for s in sorted(sources)
        ],
        "allowlist": allowlist,
    }
    out.update(over)
    return out


def run(payload: dict[str, Any]) -> ModelAwaitingReplyResult:
    result = HandlerAwaitingReply().handle_raw(payload)
    assert isinstance(result, ModelAwaitingReplyResult), result
    return result


def test_awaiting_reply_compute_contract_binds_models_handler_and_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    binding = contract["handler"]
    handler = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeAwaitingReplyCompute, handler)
    assert contract["node_type"] == "compute"
    assert contract["event_bus"]["subscribe_topics"] == [
        contract["runtime_dispatch"]["command_topic"]
    ]
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    for side in ("input_model", "output_model"):
        module = importlib.import_module(contract[side]["module"])
        assert hasattr(module, contract[side]["name"])
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "node_awaiting_reply_compute" in pyproject


def test_awaiting_reply_compute_returns_typed_items_with_wait_and_source_status() -> (
    None
):
    result = run(bundle(item()))
    assert result.status == "decided"
    [waiting] = result.waiting
    assert waiting.wait_hours == 3.0
    assert waiting.source.value == "slack"
    assert waiting.person == "Owner One"
    assert waiting.drafted_reply == "The orchestrator lane owns it."
    assert waiting.escalation_stage == 0
    assert result.sources[0].status is EnumReadStatus.OK
    assert result.allowlist_size == 1


def test_awaiting_reply_compute_orders_oldest_wait_first() -> None:
    older = item(
        link="https://slack.example/D2/p1",
        conversation_id="D2",
        posted_at=(NOW - timedelta(hours=9)).isoformat(),
    )
    result = run(bundle(item(), older))
    assert next(w.link for w in result.waiting).endswith("D2/p1")


def test_awaiting_reply_compute_failed_source_is_reported_and_forces_escalate() -> None:
    payload = bundle(item())
    payload["source_reads"] = [{"source": "slack", "ok": False, "items_read": 1}]
    result = run(payload)
    assert result.sources[0].status is EnumReadStatus.FAILED
    assert result.waiting[0].reply_class is EnumReplyClass.ESCALATE
    assert "source" in result.waiting[0].basis


def test_awaiting_reply_compute_zero_without_control_is_not_a_clean_inbox() -> None:
    payload = bundle()
    payload["source_reads"] = [{"source": "slack", "ok": True, "items_read": 0}]
    assert run(payload).sources[0].status is EnumReadStatus.EMPTY_UNVERIFIED
    payload["source_reads"] = [
        {"source": "slack", "ok": True, "items_read": 0, "control_rows": 12}
    ]
    assert run(payload).sources[0].status is EnumReadStatus.OK


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ok", True),
        ("Sure. Thank you", True),
        ("👍", True),
        ("thanks!", True),
        ("ok, can you review?", False),
        ("Which lane owns this?", False),
    ],
)
def test_awaiting_reply_compute_bare_acknowledgement(text: str, expected: bool) -> None:
    assert is_bare_acknowledgement(text) is expected


@pytest.mark.parametrize(
    ("missing"),
    [
        "source",
        "conversation_id",
        "link",
        "author",
        "author_is_bot",
        "text",
        "posted_at",
        "operator_replied_since",
    ],
)
def test_awaiting_reply_compute_missing_field_is_refused_naming_the_field(
    missing: str,
) -> None:
    broken = item()
    del broken[missing]
    result = HandlerAwaitingReply().handle_raw(bundle(broken))
    assert isinstance(result, ModelAwaitingReplyRefusal)
    assert any(e.field == f"items.0.{missing}" for e in result.errors)


def test_awaiting_reply_compute_naive_timestamp_is_refused() -> None:
    result = HandlerAwaitingReply().handle_raw(
        bundle(item(posted_at="2026-11-20T12:00:00"))
    )
    assert isinstance(result, ModelAwaitingReplyRefusal)
    assert result.errors[0].field == "items.0.posted_at"


def test_awaiting_reply_compute_item_from_unread_source_is_refused() -> None:
    payload = bundle(item())
    payload["source_reads"] = []
    result = HandlerAwaitingReply().handle_raw(payload)
    assert isinstance(result, ModelAwaitingReplyRefusal)


def test_awaiting_reply_compute_ladder_stage_comes_from_wait_and_logged_rows() -> None:
    esc = item(author_id="U-OTHER", posted_at=(NOW - timedelta(hours=80)).isoformat())
    payload = bundle(esc)
    first = run(payload).waiting[0]
    assert (first.escalation_stage, first.stages_to_fire) == (2, [1, 2])
    payload["ladder_rows"] = [{"link": esc["link"], "stage": 1}]
    second = run(payload).waiting[0]
    assert (second.escalation_stage, second.stages_to_fire) == (2, [2])
    mid = bundle(
        item(author_id="U-OTHER", posted_at=(NOW - timedelta(hours=30)).isoformat())
    )
    assert run(mid).waiting[0].escalation_stage == 1
    fresh = bundle(item(author_id="U-OTHER"))
    assert run(fresh).waiting[0].escalation_stage == 0


def test_awaiting_reply_compute_golden_roundtrip_through_json() -> None:
    result = run(bundle(item()))
    again = ModelAwaitingReplyResult.model_validate(
        json.loads(result.model_dump_json())
    )
    assert again == result


def test_awaiting_reply_compute_excluded_reasons_are_typed() -> None:
    result = run(bundle(item(author_is_bot=True)))
    assert result.excluded[0].reason is EnumExclusionReason.BOT
