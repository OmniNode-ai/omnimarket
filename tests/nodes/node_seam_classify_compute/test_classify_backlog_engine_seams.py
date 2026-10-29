# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Backlog admission and lane model seam classification tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_seam_classify_compute.handlers.classify_backlog_engine_seams import (
    BACKLOG_ENGINE_OPERATIONS,
    classify_sm_00,
    classify_sm_01,
    classify_sm_02,
)
from omnimarket.nodes.node_seam_classify_compute.handlers.handler_seam_classify import (
    HandlerSeamClassify,
)
from omnimarket.nodes.node_seam_classify_compute.models.model_seam_classify_request import (
    ModelSeamClassifyRequest,
)

pytestmark = pytest.mark.unit


def _item(key: str, required_engine: str = "claude") -> dict[str, object]:
    return {
        "key": key,
        "kind": "ticket",
        "repo": "omnimarket",
        "ticket": "OMN-21459",
        "title": "Classify backlog engine seams",
        "required_engine": required_engine,
    }


def _facts(name: str, values: object) -> dict[str, object]:
    return {
        name: values,
        "window_start": "2026-10-27T00:00:00+00:00",
        "window_end": "2026-10-28T00:00:00+00:00",
    }


def test_sm_00_counts_malformed_items_and_reports_their_keys() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    items = [
        _item("bad-engine", "whatever is cheapest"),
        _item(""),
        _item("good-1"),
        _item("good-2"),
        _item("good-3"),
    ]

    result = classify_sm_00(
        ModelSeamClassifyRequest(
            operation="classify_sm_00",
            facts=_facts("items", items),
            now=now,
        )
    )

    assert len(result.readings) == 1
    reading = result.readings[0]
    assert reading.seam_id == "SM-00"
    assert reading.crossings == 5
    assert reading.mismatched == 2
    assert reading.unreadable == 0
    assert reading.examples == ("bad-engine", "item-1")
    assert reading.read_at == now
    assert reading.window.start == datetime(2026, 10, 27, tzinfo=UTC)
    assert reading.window.end == now


def test_sm_00_accepts_the_claude_engine_family() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)

    result = classify_sm_00(
        ModelSeamClassifyRequest(
            operation="classify_sm_00",
            facts=_facts("items", [_item("family-claude", "claude")]),
            now=now,
        )
    )

    reading = result.readings[0]
    assert reading.crossings == 1
    assert reading.mismatched == 0
    assert reading.unreadable == 0


@pytest.mark.parametrize(
    ("operation", "model_fact", "seam_id"),
    [
        ("classify_sm_01", "brief_model", "SM-01"),
        ("classify_sm_02", "claim_model", "SM-02"),
    ],
)
@pytest.mark.parametrize(
    ("model", "escalation_reason", "mismatched"),
    [
        ("sonnet", None, 0),
        ("opus", None, 1),
        ("opus", "sonnet-failed:lane-1", 0),
        ("codex", None, 1),
    ],
)
def test_lane_models_follow_the_adopted_policy(
    operation: str,
    model_fact: str,
    seam_id: str,
    model: str,
    escalation_reason: str | None,
    mismatched: int,
) -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    lane: dict[str, object] = {
        "key": "lane-1",
        "kind": "ticket",
        "required_engine": "claude",
        model_fact: model,
    }
    if escalation_reason is not None:
        lane["escalation_reason"] = escalation_reason

    request = ModelSeamClassifyRequest(
        operation=operation,
        facts=_facts("lanes", [lane]),
        now=now,
    )
    result = (
        classify_sm_01(request)
        if operation == "classify_sm_01"
        else classify_sm_02(request)
    )

    reading = result.readings[0]
    assert reading.seam_id == seam_id
    assert reading.crossings == 1
    assert reading.mismatched == mismatched
    assert reading.unreadable == 0
    assert reading.examples == (("lane-1",) if mismatched else ())
    assert reading.read_at == now


def test_sm_00_refuses_a_model_outside_the_packaged_ladder() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    # Ladder validation context is not exposed through classifier facts.
    result = classify_sm_00(
        ModelSeamClassifyRequest(
            operation="classify_sm_00",
            facts=_facts("items", [_item("future-rung", "claude-opus-9")]),
            now=now,
        )
    )

    reading = result.readings[0]
    assert reading.crossings == 1
    assert reading.mismatched == 1
    assert reading.examples == ("future-rung",)


def test_sm_00_requires_items() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    facts = _facts("items", [])
    del facts["items"]

    with pytest.raises(ValueError, match="items"):
        classify_sm_00(
            ModelSeamClassifyRequest(
                operation="classify_sm_00",
                facts=facts,
                now=now,
            )
        )


def test_registered_operations_dispatch_sm_00() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    request = ModelSeamClassifyRequest(
        operation="classify_sm_00",
        facts=_facts("items", [_item("dispatch")]),
        now=now,
    )

    result = HandlerSeamClassify(BACKLOG_ENGINE_OPERATIONS).handle(request)

    assert result == classify_sm_00(request)
    assert result.readings[0].seam_id == "SM-00"
    assert result.readings[0].crossings == 1
    assert result.readings[0].mismatched == 0
