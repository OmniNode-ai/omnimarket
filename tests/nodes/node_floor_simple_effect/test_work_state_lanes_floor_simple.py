# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor attributes a lane to a row from its CLAIM's cells in the work-state projection (OMN-22935 AC2).

No owner is typed into the floor overlay: the lanes come from the projection node's output, built from the ledger, and a
lane whose CLAIM names a row by its ``cause`` or ``owns=floor:`` cell shows on that row after one publish. The failing
control is the same ledger with the cells taken away, which draws the row red with nobody on it. The board read runs the
whole cycle: the projection file is read from the registry home and the simple floor is written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.floor_17a import (
    ModelFloorCauseIn,
    ModelFloorLane,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
)
from omnimarket.models.work_state_projection import (
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateResult,
    ModelWorkStateRules,
)
from omnimarket.nodes.node_floor_simple_compute.handlers import (
    HandlerFloorSimple,
    check_red_rule,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    merge_lanes,
    read_work_state,
    work_state_lanes,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    build_home,
    environ,
    overlay_text,
)

pytestmark = pytest.mark.unit

EXAMPLE_OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_work_state_projection_compute/overlay.example.yaml"
)
RULES = load_structured_overlay(
    "node_work_state_projection_compute",
    ModelWorkStateRules,
    explicit=yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8")),
)
AT = datetime(2026, 11, 7, 13, 0, tzinfo=UTC)
STOPPAGES = tuple(
    ModelFloorCauseIn(
        id=f"stoppage:{name}",
        title=name,
        detail="seen",
        size=3,
        size_unit="occurrences",
    )
    for name in ("lab-idle", "job-exit", "quiet-host")
)


def claim(lane: str, ago: float, cells: str = "") -> str:
    stamp = (AT - timedelta(minutes=ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} | CLAIM | lane={lane} | ticket=OMN-1{cells} | actor=claude | "
        "model=sonnet | a lane on the floor"
    )


def terminal(lane: str, ago: float) -> str:
    stamp = (AT - timedelta(minutes=ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp} | TERMINAL | lane={lane} | ticket=OMN-1 | outcome=done | done"


def project(*rows: str) -> ModelWorkStateResult:
    parsed = tuple(evidence_row_from_text(text) for text in rows)
    assert all(r is not None for r in parsed)
    return HandlerWorkStateProjection().handle(
        ModelWorkStateRequest(
            now=AT,
            rules=RULES,
            scope=ModelWorkScope(),
            ledger_rows=tuple(r for r in parsed if r is not None),
        )
    )


# cause-lane names a holdup by its cause cell, owns-lane by owns=floor:, ended-lane wrote the cell and then closed, and
# no-cell-lane works the same ticket with no cell at all.
ROWS = (
    claim("cause-lane", 20, " | cause=stoppage:lab-idle"),
    claim("owns-lane", 15, " | owns=floor:stoppage:job-exit"),
    claim("ended-lane", 40, " | cause=stoppage:quiet-host"),
    terminal("ended-lane", 10),
    claim("no-cell-lane", 5),
)


def floor_doc(lanes: tuple[ModelFloorLane, ...]) -> ModelFloorSimple:
    overlay = ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )
    assert not overlay.causes.owners.get("stoppage:lab-idle")
    return HandlerFloorSimple().handle(
        ModelFloorSimpleInput(
            now=NOW,
            overlay=overlay,
            open_prs=10,
            open_prs_before=12,
            merges_in_window=4,
            watcher_read_at=NOW - timedelta(minutes=1),
            slots_working=5,
            slots_total=20,
            record_read_at=NOW - timedelta(minutes=1),
            stoppages=STOPPAGES,
            lanes=lanes,
            lab=None,
            receipts=None,
            go_given_through=0,
        )
    )


def cause(doc: ModelFloorSimple, cause_id: str) -> tuple[str, tuple[str, ...]]:
    found = next(c for c in doc.causes if c.id == cause_id)
    return found.state, found.lanes


def test_golden_a_lane_shows_on_the_row_its_claim_names_with_no_owner_in_the_overlay() -> (
    None
):
    lanes = work_state_lanes(project(*ROWS))
    doc = floor_doc(lanes)
    assert cause(doc, "stoppage:lab-idle") == ("amber", ("cause-lane",))
    assert cause(doc, "stoppage:job-exit") == ("amber", ("owns-lane",))
    assert check_red_rule(doc, lanes).violations == ()


def test_error_a_lane_that_closed_or_names_no_row_is_on_nothing() -> None:
    doc = floor_doc(work_state_lanes(project(*ROWS)))
    assert cause(doc, "stoppage:quiet-host") == ("red", ())
    assert all("no-cell-lane" not in c.lanes for c in doc.causes)


def test_error_the_control_without_the_claim_cells_draws_every_row_red() -> None:
    bare = tuple(
        row.replace(" | cause=", " | causz=").replace(" | owns=", " | ownz=")
        for row in ROWS
    )
    doc = floor_doc(work_state_lanes(project(*bare)))
    for cause_id in ("stoppage:lab-idle", "stoppage:job-exit"):
        assert cause(doc, cause_id) == ("red", ())


def test_merge_keeps_the_records_process_reading_and_adds_the_claim_cells() -> None:
    recorded = (ModelFloorLane(lane="cause-lane", liveness="alive"),)
    merged = merge_lanes(recorded, work_state_lanes(project(*ROWS)))
    by_name = {lane.lane: lane for lane in merged}
    assert by_name["cause-lane"].liveness == "alive"
    assert by_name["cause-lane"].owns == ("stoppage:lab-idle",)
    assert by_name["cause-lane"].ticket == "OMN-1"
    assert "owns-lane" in by_name


def test_board_read_a_published_projection_puts_the_lane_on_the_row(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    path = home / ".onex_state/work-state/work-state.json"
    path.parent.mkdir(parents=True)
    path.write_text(project(*ROWS).model_dump_json(), encoding="utf-8")
    assert read_work_state(path) is not None
    result = HandlerFloorSimpleTick(sink=InMemoryEmissionSink(), clock=lambda: NOW).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=environ(home)
    )
    assert result.ok
    doc = ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )
    assert doc.sources_read["work_state_projection"] is True
    assert "source:work_state_projection" not in {c.id for c in doc.causes}


def test_error_board_read_an_absent_projection_is_a_cause_not_an_empty_lane_list(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    result = HandlerFloorSimpleTick(sink=InMemoryEmissionSink(), clock=lambda: NOW).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=environ(home)
    )
    assert result.ok
    doc = ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )
    assert doc.sources_read["work_state_projection"] is False
    assert "source:work_state_projection" in {c.id for c in doc.causes}
