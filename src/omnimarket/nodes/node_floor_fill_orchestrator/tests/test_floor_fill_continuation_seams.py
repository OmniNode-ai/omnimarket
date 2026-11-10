# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the continuation of stopped lanes (OMN-23416), each read against the real code of the other side.

1. the floor record's stopped lane (node_factory_floor_compute) -> node_floor_fill_candidates_compute;
2. node_floor_fill_candidates_compute's ``stopped`` result and ``reports`` request field -> this orchestrator;
3. this orchestrator's ``floor_fill.stopped_runner_command`` and brief file -> the lane start node's entry (lane startnode-0215,
   ``runtime_remote_lane_start.parser`` and ``ModelRemoteLaneStartRequest``);
4. the start node's CLAIM cells -> this orchestrator's ledger read (``claim_found`` on ``key=``).

Every check has a control that changes one name or shape on one side and goes red.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.floor_fill import (
    ModelFloorFillCandidatesRequest,
    ModelFloorFillStopped,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    load_floor_fill_config,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.adapters import (
    DirectoryFloorBriefs,
    LedgerRowsFloorLedger,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedBriefs,
    ScriptedFacts,
    floor_record,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    OVERLAY,
    T0,
    harness,
)
from omnimarket.nodes.node_floor_fill_reducer.handlers.handler_floor_fill_reducer import (
    lane_name,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.runtime_remote_lane_start import (
    parser,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parent.parent
LANE = "prev-0100"
REPORT_PATH = ".claude_scratch/reports/prev-0100/result.md"
REPORT_TEXT = "PREVIOUS-RESULT-TEXT: parser done, tests left"
COMPUTE_OVERLAY = {
    key: OVERLAY[key]
    for key in (
        "max_age_minutes",
        "max_fill_lanes_per_line",
        "sent_window_minutes",
        "stopped_max_lanes",
        "stopped_report_chars",
        "stopped_continue_rule",
    )
}


def _floor_lane() -> dict[str, Any]:
    """A stopped lane shaped the way the published record carries it."""
    return {
        "lane": LANE,
        "ticket": "OMN-23001",
        "tickets": ["OMN-23001"],
        "outcome": "partial",
        "ended_at": "2026-11-07T11:30:00Z",
        "age_minutes": 30,
        "names": ["branch"],
        "branch": "omn-23001-slice",
        "head": "abc123def456",
        "prs": [],
        "rows": [
            "2026-11-07T11:30:00Z TERMINAL outcome=partial: pushed slice, tests left",
        ],
        "report_path": REPORT_PATH,
    }


def _compute(stopped: dict[str, Any]) -> Any:
    record = floor_record({}, generated_at=T0, stopped=[stopped])
    request = ModelFloorFillCandidatesRequest(
        now=T0.isoformat(), floor_record=record, **COMPUTE_OVERLAY
    )
    return HandlerFloorFillCandidates().handle(request)


def _filled_command(brief: str) -> list[str]:
    section = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())[
        "floor_fill"
    ]
    values = {
        "lane": lane_name("floor:stopped:" + LANE, 1),
        "key": "floor:stopped:" + LANE,
        "ticket": "OMN-23001",
        "attempt": "1",
        "brief": brief,
    }
    out = []
    for element in section["stopped_runner_command"][1:]:
        for name, value in values.items():
            element = element.replace("{" + name + "}", value)
        out.append(element)
    return out


# --------------------------------------------------------------- seam 1: floor record -> candidates compute


def test_seam_floor_stopped_lane_reaches_the_candidate_with_its_report_path() -> None:
    result = _compute(_floor_lane())
    (stopped,) = result.stopped
    assert (stopped.lane, stopped.ticket, stopped.branch, stopped.head) == (
        LANE,
        "OMN-23001",
        "omn-23001-slice",
        "abc123def456",
    )
    assert REPORT_PATH in stopped.brief
    assert "pushed slice, tests left" in stopped.brief


def test_seam_control_a_renamed_report_path_is_seen_by_the_join() -> None:
    renamed = {
        ("report_file" if key == "report_path" else key): value
        for key, value in _floor_lane().items()
    }
    (stopped,) = _compute(renamed).stopped
    assert REPORT_PATH not in stopped.brief


def test_seam_control_a_renamed_lane_field_drops_the_candidate() -> None:
    renamed = {("lane_id" if k == "lane" else k): v for k, v in _floor_lane().items()}
    assert _compute(renamed).verdict == "FLOOR-UNREAD"


# --------------------------------------------------------------- seam 2: compute result -> orchestrator


def test_seam_the_fields_the_orchestrator_reads_and_writes_exist_on_the_compute_models() -> (
    None
):
    assert {"key", "lane", "ticket", "branch", "head", "brief"} <= set(
        ModelFloorFillStopped.model_fields
    )
    assert {
        "reports",
        "entries",
        "stopped_max_lanes",
        "stopped_report_chars",
        "stopped_continue_rule",
    } <= set(ModelFloorFillCandidatesRequest.model_fields)


def _tick_with_stopped() -> Any:
    h = harness(
        lines={},
        facts=ScriptedFacts(floor_record({}, generated_at=T0, stopped=[_floor_lane()])),
        briefs=ScriptedBriefs(reports={REPORT_PATH: REPORT_TEXT}),
    )
    h.orchestrator().run(EnumFloorFillCommand.TICK)
    return h


def test_seam_the_real_compute_result_starts_one_lane_end_to_end() -> None:
    h = _tick_with_stopped()
    (start,) = h.starter.starts
    assert start.key == "floor:stopped:" + LANE
    assert REPORT_TEXT in h.briefs.saved[start.lane]


def test_seam_control_the_compute_dropping_its_stopped_list_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = HandlerFloorFillCandidates.handle

    def without_stopped(self: Any, request: Any) -> Any:
        return real(self, request).model_copy(update={"stopped": ()})

    monkeypatch.setattr(HandlerFloorFillCandidates, "handle", without_stopped)
    assert _tick_with_stopped().starter.starts == []


def test_seam_control_the_compute_ignoring_reports_leaves_the_result_out_of_the_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = HandlerFloorFillCandidates.handle

    def without_reports(self: Any, request: Any) -> Any:
        return real(self, request.model_copy(update={"reports": ()}))

    monkeypatch.setattr(HandlerFloorFillCandidates, "handle", without_reports)
    h = _tick_with_stopped()
    (start,) = h.starter.starts
    assert REPORT_TEXT not in h.briefs.saved[start.lane]


# --------------------------------------------------------------- seam 3: orchestrator -> lane start entry


def test_seam_the_filled_command_parses_on_the_start_entry_and_its_brief_is_the_saved_text(
    tmp_path: Path,
) -> None:
    briefs = DirectoryFloorBriefs(str(tmp_path), str(tmp_path / "briefs"))
    path = briefs.save_brief(lane_name("floor:stopped:" + LANE, 1), REPORT_TEXT)
    args = parser().parse_args(_filled_command(path))
    assert Path(args.brief).read_text(encoding="utf-8") == REPORT_TEXT
    assert (args.key, args.ticket, args.attempt) == (
        "floor:stopped:" + LANE,
        "OMN-23001",
        1,
    )
    assert args.lane == lane_name("floor:stopped:" + LANE, 1)


def test_seam_control_a_flag_the_entry_does_not_take_is_refused() -> None:
    command = [
        ("--brief-file" if e == "--brief" else e) for e in _filled_command("/tmp/x")
    ]
    with pytest.raises(SystemExit):
        parser().parse_args(command)


def test_seam_the_example_overlay_is_a_valid_config_whose_command_the_entry_accepts() -> (
    None
):
    section = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())[
        "floor_fill"
    ]
    config = load_floor_fill_config(section)
    assert any("{brief}" in element for element in config.stopped_runner_command)


# --------------------------------------------------------------- seam 4: start node CLAIM -> fill ledger read


class _Rows:
    def __init__(self, rows: tuple[str, ...]) -> None:
        self.rows = rows

    def rows_for_lane(self, lane: str) -> tuple[str, ...]:
        return self.rows


def _claim_row(key_cell: str) -> str:
    cells = [  # the CLAIM cells the start node writes, in its order
        f"lane={lane_name('floor:stopped:' + LANE, 1)}",
        "ticket=OMN-23001",
        "host=h1",
        "key=floor:stopped:" + LANE,
        "run=r1",
    ]
    cells = [key_cell if c.startswith("key=") else c for c in cells]
    return " | ".join(["2026-11-10T00:00:00Z", "CLAIM", *cells, "text"])


def test_seam_the_start_nodes_claim_row_is_found_by_the_fill_on_its_state_key() -> None:
    lane = lane_name("floor:stopped:" + LANE, 1)
    ledger = LedgerRowsFloorLedger(_Rows((_claim_row("key=floor:stopped:" + LANE),)))
    assert ledger.claim_found(lane, "floor:stopped:" + LANE)
    assert not ledger.claim_found(lane, "floor:stopped:other")


def test_seam_control_a_renamed_key_cell_is_not_found() -> None:
    lane = lane_name("floor:stopped:" + LANE, 1)
    ledger = LedgerRowsFloorLedger(_Rows((_claim_row("fkey=floor:stopped:" + LANE),)))
    assert not ledger.claim_found(lane, "floor:stopped:" + LANE)


def test_the_stored_state_names_the_stopped_lane_under_the_floor_key_form() -> None:
    h = _tick_with_stopped()
    stored = json.loads(h.store.load(h.config.scope).payload or "{}")
    assert list(stored["entries"]) == ["floor:stopped:" + LANE]
