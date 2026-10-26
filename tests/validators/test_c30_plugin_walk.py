# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the real plugin walk and its criterion falsifiers with the lab grader."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_plugin_walk_evidence_effect.handlers.handler_plugin_walk_evidence import (
    delegate_receipts,
    grade_negative,
    grade_positive,
)

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).parents[1] / "fixtures/c30_plugin_walk_2026_10_25.json"


def _walk() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_recorded_published_plugin_walk_has_one_successful_metered_store_row() -> None:
    walk = _walk()
    assert walk["plugin"] == {"source": "onex@omninode-tools", "version": "0.3.2"}
    assert grade_positive(walk["positive"], walk["readback"]) == [
        "b4ba8b70-1d57-418d-b788-fc1918483fef"
    ]
    receipt = delegate_receipts(walk["positive"])[0]
    assert receipt["result"]["model_name"] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert receipt["result"]["metrics"]["total_tokens"] == 321


@pytest.mark.parametrize("table", ["delegation_events", "llm_call_metrics"])
@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_duplicate_store_rows_are_refused(table: str, count: int) -> None:
    walk = _walk()
    rows = next(iter(walk["readback"].values()))
    rows[table] = rows[table] * count
    with pytest.raises(ValueError, match="exactly one row"):
        grade_positive(walk["positive"], walk["readback"])


@pytest.mark.parametrize("field", ["terminal_ok", "total_tokens"])
def test_a_provider_failure_or_zero_tokens_is_refused(field: str) -> None:
    walk = _walk()
    rows = next(iter(walk["readback"].values()))
    table = "delegation_events" if field == "terminal_ok" else "llm_call_metrics"
    rows[table][0][field] = 0
    with pytest.raises(ValueError, match=r"terminal_ok|tokens"):
        grade_positive(walk["positive"], walk["readback"])


def test_a_correlation_id_in_assistant_prose_is_not_a_tool_receipt() -> None:
    walk = _walk()
    events = [
        walk["positive"][0],
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "correlation_id b4ba8b70-1d57-418d-b788-fc1918483fef",
                    }
                ]
            },
        },
    ]
    with pytest.raises(ValueError, match="no onex delegate tool-result"):
        grade_positive(events, walk["readback"])


def test_recorded_no_skills_plugin_walk_has_no_skill_or_correlation_id() -> None:
    events = _walk()["negative"]
    # Claude exits successfully in this negative arm too; it is not a pass signal.
    assert events[-1]["subtype"] == "success"
    grade_negative(events)


def test_negative_arm_that_lists_the_skill_is_refused() -> None:
    events = _walk()["negative"]
    events[0]["skills"].append("onex:delegate")
    with pytest.raises(ValueError, match="negative walk lists"):
        grade_negative(events)


def test_negative_arm_with_a_delegate_correlation_id_is_refused() -> None:
    walk = _walk()
    events = copy.deepcopy(walk["negative"])
    events.extend(walk["positive"][1:-1])
    with pytest.raises(ValueError, match="negative walk has"):
        grade_negative(events)
