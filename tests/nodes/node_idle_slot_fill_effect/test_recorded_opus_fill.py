# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reproduce the Opus fill using the actual 2026-10-26 brief names and ledger rows."""

from __future__ import annotations

import json
from pathlib import Path

from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
    HandlerIdleSlotFill,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models import ModelIdleSlotFillRequest


def test_recorded_1959_claude_fills_stay_within_ceiling():
    recorded = json.loads(
        (Path(__file__).parent / "fixtures/opus_fill_20261026.json").read_text()
    )
    backlog = []
    expected = []
    for record in recorded["records"]:
        lane = record["brief_name"].removesuffix(".brief.md")
        kind = record["kind"]
        assert record["observed_model"] == "opus"
        expected.append(lane)
        # Only the kind and exact brief name affect this reproduction; ledger provenance
        # is preserved unchanged in the private lane handoff, outside this public package.
        name = lane.removeprefix(f"fill-{kind}-").removesuffix("-20261026T1959Z")
        backlog.append(
            {
                "kind": kind,
                "key": name,
                "repo": "omnimarket",
                "ticket": "OMN-21396",
                "fact": record["brief_name"],
            }
        )
    result = HandlerIdleSlotFill().handle(
        ModelIdleSlotFillRequest(
            now="2026-10-26T19:59:00Z",
            hosts=[{"name": "h201", "admission": {"claude_slots": len(backlog)}}],
            lines=["MISSING lab-headroom:h201 | free Claude slots 4"],
            backlog=backlog,
        )
    )
    assert [send["lane"] for send in result.sends] == expected
    assert [send["model"] for send in result.sends] == ["sonnet"] * len(backlog)
    assert all(
        "ROUTE: model=sonnet effort=high" in send["brief"] for send in result.sends
    )
    assert all(
        item["fact"] in send["brief"]
        for item, send in zip(backlog, result.sends, strict=True)
    )
