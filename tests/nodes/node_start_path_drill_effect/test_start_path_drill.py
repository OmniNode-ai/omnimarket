# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start-path census and refusal drill (OMN-23306): its reading, its observe log and the named breaks that fail it."""

from __future__ import annotations

import pytest
import yaml

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_start_path_drill_effect.handlers.handler_start_path_drill import (
    CONTRACT,
    load_declared_paths,
)
from omnimarket.nodes.node_start_path_drill_effect.models.model_start_path_drill import (
    ModelStartPathDrillCommand,
)
from omnimarket.nodes.node_start_path_drill_effect.overlay import EnumPathBinding
from tests.nodes.node_start_path_drill_effect.drill_support import (
    EXPECTED_PATHS,
    World,
    example_overlay,
)

pytestmark = pytest.mark.unit

RUN = ModelStartPathDrillCommand(correlation_id="drill-1", write=True)


def test_start_path_drill_contract_declares_every_path_of_the_plan() -> None:
    assert load_declared_paths() == EXPECTED_PATHS
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert len(contract["start_paths"]) == len(set(contract["start_paths"])) == 15


def test_start_path_drill_census_holds_every_path_and_every_planted_start_is_refused() -> (
    None
):
    world = World()
    result = world.handler().handle(RUN)
    assert result.verdict is EnumReadingVerdict.WORKING, result.reasons
    assert result.census_paths == 15
    assert result.census_missing == ()
    assert result.refused == 15
    assert result.admitted == 0
    assert result.control_admitted == 15
    assert [p.path for p in result.paths] == list(EXPECTED_PATHS)
    assert all(p.planted_outcome == "refused" for p in result.paths)
    assert all(p.control_outcome == "admitted" for p in result.paths)


def test_start_path_drill_writes_one_reading_row_with_the_counts() -> None:
    world = World()
    result = world.handler().handle(RUN)
    assert result.ledger == "written"
    assert len(world.reading_ledger.lines) == 1
    row = world.reading_ledger.lines[0]
    for cell in (
        "| STATUS |",
        "src=start-path-drill",
        "drill=1",
        "verdict=WORKING",
        "census=15",
        "refused=15",
        "admitted=0",
        "control_admitted=15",
        "census_missing=none",
        "log_written=30",
        "log_failed=0",
    ):
        assert cell in row, cell


def test_start_path_drill_observe_log_has_one_row_per_decision_tagged_drill() -> None:
    world = World()
    world.handler().handle(RUN)
    rows = world.log_ledger.lines
    assert len(rows) == 30
    assert all("src=start-admission-observe" in r and "drill=1" in r for r in rows)
    planted = [r for r in rows if "outcome=refused" in r]
    control = [r for r in rows if "outcome=admitted" in r]
    assert len(planted) == len(control) == 15
    assert all("proceed=0" in r for r in planted)
    assert all("proceed=1" in r for r in control)
    assert {p for r in rows for p in EXPECTED_PATHS if f"path={p} " in r + " "} == set(
        EXPECTED_PATHS
    )


def test_start_path_drill_a_probe_run_appends_nothing_to_the_reading_ledger() -> None:
    world = World()
    result = world.handler(write=False).handle(
        ModelStartPathDrillCommand(correlation_id="drill-p", write=False)
    )
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.ledger == "not-written"
    assert world.reading_ledger.lines == []


def test_start_path_drill_a_dead_path_is_counted_and_not_drilled() -> None:
    world = World()
    census = tuple(
        p.model_copy(update={"binding": EnumPathBinding.DEAD})
        if p.id == "cloud_lanes"
        else p
        for p in world.overlay.census
    )
    world.overlay = world.overlay.model_copy(update={"census": census})
    result = world.handler().handle(RUN)
    assert result.verdict is EnumReadingVerdict.WORKING, result.reasons
    assert (result.dead, result.refused, result.control_admitted) == (1, 14, 14)
    assert "cloud_lanes" not in [p.path for p in result.paths]


# ---- the named breaks: each turns the reading red ----


@pytest.mark.parametrize("path", EXPECTED_PATHS)
def test_start_path_drill_break_a_start_path_left_out_of_the_census_fails_the_reading(
    path: str,
) -> None:
    world = World()
    world.break_path_left_out_of_the_census(path)
    result = world.handler().handle(RUN)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.census_missing == (path,)
    assert f"census-missing-{path}" in result.reasons
    assert f"census_missing={path}" in world.reading_ledger.lines[0]


def test_start_path_drill_break_admission_names_a_path_the_census_lacks() -> None:
    world = World()
    world.break_admission_names_a_path_the_census_lacks("a_new_path")
    result = world.handler().handle(RUN)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.census_missing == ("a_new_path",)


def test_start_path_drill_break_a_gate_that_admits_everything_is_a_bypass() -> None:
    world = World()
    world.break_gate_admits_everything()
    result = world.handler().handle(RUN)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert (result.refused, result.admitted) == (0, 15)
    assert any(
        r.startswith("planted-start-admitted-lane_runner") for r in result.reasons
    )
    assert "admitted=15" in world.reading_ledger.lines[0]


def test_start_path_drill_break_a_gate_that_refuses_everything_admits_no_control() -> (
    None
):
    world = World()
    world.break_gate_refuses_everything()
    result = world.handler().handle(RUN)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.control_admitted == 0
    assert any(r.startswith("control-start-not-admitted-") for r in result.reasons)


def test_start_path_drill_break_an_observe_log_that_takes_no_rows_fails_the_reading() -> (
    None
):
    world = World()
    world.break_observe_log_refuses_rows()
    result = world.handler().handle(RUN)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.log_failed == 30
    assert result.log_written == 0
    assert "observe-log-append-failed-30" in result.reasons
    assert world.log_ledger.lines == []


def test_start_path_drill_break_an_empty_census_fails_the_reading() -> None:
    world = World()
    world.overlay = world.overlay.model_copy(update={"census": ()})
    result = world.handler().handle(RUN)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert "census-empty" in result.reasons


def test_start_path_drill_an_append_that_does_not_land_is_a_failed_run() -> None:
    world = World()
    world.reading_ledger.append_status = "refused"
    result = world.handler().handle(RUN)
    assert result.failed
    assert result.ledger == "append-failed:refused"


def test_start_path_drill_a_writing_run_without_an_appender_is_a_typed_failure() -> (
    None
):
    result = World().handler(write=False).handle(RUN)
    assert result.status == "failed"
    assert result.failure == "no-appender-for-a-writing-run"


def test_start_path_drill_a_census_path_twice_is_refused_by_the_overlay() -> None:
    document = example_overlay().model_dump(mode="json")
    document["census"].append(document["census"][0])
    with pytest.raises(ValueError, match="twice"):
        type(example_overlay()).model_validate(document)
