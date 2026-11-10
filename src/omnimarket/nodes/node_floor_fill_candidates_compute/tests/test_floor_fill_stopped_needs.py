# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_fill_candidates_compute: the ask a stopped entry's ``needs`` cell carries into its brief."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.models.floor_fill import (
    ModelFloorFillCandidatesRequest,
    ModelFloorFillCandidatesResult,
)
from omnimarket.models.floor_fill.model_floor_fill_candidates import ModelRecordStopped
from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.tests.support import (
    NOW,
    minutes_before,
    request,
)

pytestmark = pytest.mark.unit


def run(req: ModelFloorFillCandidatesRequest) -> ModelFloorFillCandidatesResult:
    return HandlerFloorFillCandidates().handle(req)


def stopped_entry(**overrides: Any) -> dict[str, Any]:
    return {
        "lane": "lane-a",
        "ticket": "OMN-23416",
        "tickets": ["OMN-23416"],
        "outcome": "blocked",
        "ended_at": "2026-11-02T10:30:00Z",
        "names": ["needs"],
        "needs": "rerun:omnibase_core#2302",
        "branch": "omn-1-x",
        "head": "abc1234",
        "prs": [],
        "rows": ["2026-11-02T10:30:00Z | TERMINAL | lane=lane-a | outcome=blocked"],
        "report_path": "reports/lane-a.md",
        **overrides,
    }


def stopped_record(entry: dict[str, Any]) -> str:
    return json.dumps(
        {
            "schema_id": "omni.factory-floor/1",
            "generated_at": minutes_before(NOW, 5),
            "lines": [],
            "floor": {"lanes": [], "stopped": [entry]},
        }
    )


def only_brief(text: str) -> str:
    [stopped] = run(request(floor_record=text)).stopped
    return stopped.brief


# ------------------------------------------------------------------ golden: the ask lands in the brief


def test_golden_needs_ask_is_carried_into_the_brief() -> None:
    """The brief of the one stopped entry reads the ask written in its needs cell."""
    brief = only_brief(stopped_record(stopped_entry()))
    assert "The predecessor asked for next: rerun:omnibase_core#2302" in brief


def test_golden_the_needs_line_sits_before_the_last_rows() -> None:
    """The brief carries the ask line after the Continue lane paragraph and before the last rows."""
    brief = only_brief(stopped_record(stopped_entry()))
    ask_at = brief.index("The predecessor asked for next: rerun:omnibase_core#2302")
    rows_at = brief.index("The predecessor's last rows:")
    assert brief.index("Continue lane lane-a") < ask_at < rows_at


# ------------------------------------------------------------------ error chain: a needs cell naming no ask


def test_error_chain_unparsed_needs_names_no_ask_and_stays_startable() -> None:
    """A stopped entry whose needs cell reads unparsed names no ask in its brief and stays STARTABLE."""
    result = run(request(floor_record=stopped_record(stopped_entry(needs="unparsed"))))
    [stopped] = result.stopped
    assert "asked for next" not in stopped.brief
    assert result.stopped_verdict == "STARTABLE"


def test_error_chain_no_needs_cell_names_no_ask_and_stays_startable() -> None:
    """A stopped entry written without a needs cell at all names no ask and stays STARTABLE."""
    entry = stopped_entry()
    del entry["needs"]
    result = run(request(floor_record=stopped_record(entry)))
    [stopped] = result.stopped
    assert "asked for next" not in stopped.brief
    assert result.stopped_verdict == "STARTABLE"


# ------------------------------------------------------------------ control: the record model declares the cell


def test_control_the_record_model_declares_the_needs_field() -> None:
    """ModelRecordStopped declares the needs cell the brief reads."""
    assert "needs" in ModelRecordStopped.model_fields
