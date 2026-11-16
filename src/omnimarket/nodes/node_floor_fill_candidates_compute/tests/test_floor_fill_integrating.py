# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_fill_candidates_compute reads a floor record whose items are integrating (merged, being shown running).

The factory floor compute publishes ``integrating`` beside done, active, prep, wait and stop. A reader that does not know
the word fails the whole record and answers FLOOR-UNREAD, so the fill starts no lane and no continuation. The golden
tests read a record with an integrating item and a stopped lane; the error chain shows a word no publisher declares
still fails the record; the control is the unfixed model, which reads the golden record as malformed.
"""

from __future__ import annotations

import json
from typing import Any, get_args

import pytest

from omnimarket.models.floor_fill import (
    ModelFloorFillCandidatesRequest,
    ModelFloorFillCandidatesResult,
)
from omnimarket.models.model_factory_floor_input import ItemState
from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.tests.support import (
    LINE,
    item,
    record,
    request,
    usable,
)

pytestmark = pytest.mark.unit

STOPPED_ENTRY: dict[str, Any] = {
    "lane": "lane-a",
    "ticket": "TCK-9",
    "tickets": ["TCK-9"],
    "outcome": "partial",
    "ended_at": "2026-11-02T10:30:00Z",
    "names": ["branch"],
    "branch": "branch-a",
    "head": "abc1234",
    "prs": [],
    "rows": ["2026-11-02T10:30:00Z | TERMINAL | lane=lane-a | outcome=partial"],
}


def run(req: ModelFloorFillCandidatesRequest) -> ModelFloorFillCandidatesResult:
    return HandlerFloorFillCandidates().handle(req)


def with_stopped(text: str) -> str:
    document = json.loads(text)
    document["floor"]["stopped"] = [STOPPED_ENTRY]
    return json.dumps(document)


# ------------------------------------------------------------------ golden: an integrating item does not unread the record


def test_golden_integrating_item_is_not_ready_and_the_rest_of_the_record_reads() -> (
    None
):
    text = record([item("T1", "integrating"), item("T2", "prep")])
    result = run(
        request(floor_record=text, ticket_verdicts=(usable(LINE, "T2", "TCK-2"),))
    )
    assert result.verdict == "READ"
    [line] = result.lines
    assert dict(line.not_ready_pairs())["T1"] == "integrating"
    assert [c.task_id for c in line.candidates] == ["T2"]


def test_golden_stopped_lane_is_listed_from_a_record_with_integrating_items() -> None:
    text = with_stopped(record([item("T1", "integrating"), item("T2", "prep")]))
    result = run(request(floor_record=text))
    assert result.verdict == "READ"
    assert [s.lane for s in result.stopped] == ["lane-a"]
    [line] = result.lines
    assert dict(line.not_ready_pairs())["T1"] == "integrating"


def test_golden_every_state_the_publisher_declares_reads() -> None:
    states = get_args(ItemState)
    text = record([item(f"T{n}", state) for n, state in enumerate(states)])
    result = run(request(floor_record=text))
    assert result.verdict == "READ"
    [line] = result.lines
    assert {task for task, _ in line.not_ready_pairs()} >= {
        f"T{n}" for n, state in enumerate(states) if state in ("done", "integrating")
    }


# ------------------------------------------------------------------ error chain: an unknown word still fails the record


def test_error_chain_a_state_no_publisher_declares_still_reads_malformed() -> None:
    text = record([item("T1", "unheard-of")])
    result = run(request(floor_record=text))
    assert result.verdict == "FLOOR-UNREAD"
    assert result.unread_cause == "malformed"
    assert result.stopped == ()
