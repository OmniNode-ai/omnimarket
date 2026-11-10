# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the delegation board's lab-node reader and the scenario nodes that write what it reads (OMN-23407).

Three pieces share one thing: the ledger STATUS row of a scenario run, written under the node contract's lane. The scenario node
writes it, ``node_delegation_scenario_days_effect`` counts green days from it under its own table of lanes, and the board reader
(``read_lab_node_scenario``) takes the scenario's result from it under the lane the deployment overlay names. Each test below
fails when either side changes shape: a lane renamed on one side, a row cell renamed, a conclusion that no longer reads back.

The rows here come from the real R2 handler (a scripted ``onex`` as the product behaves), not from a hand-built string.
"""

from __future__ import annotations

from datetime import timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.delegation_scenario_runtime import InMemoryScenarioEventSink
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    ModelScenarioResult,
)
from omnimarket.models.model_point_query import ModelPointQuery
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_scenario_r2 import (
    HandlerScenarioR2,
)
from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_sources import (
    read_lab_node_scenario,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelScenarioDecl,
    ModelScenarioRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import ModelPointAnswer
from tests.nodes.node_delegation_scenario_r2_effect.test_golden_chain_scenario_r2_effect import (
    NOW as R2_RUN_AT,
)
from tests.nodes.node_delegation_scenario_r2_effect.test_golden_chain_scenario_r2_effect import (
    RUNTIME as R2_RUNTIME,
)
from tests.nodes.node_delegation_scenario_r2_effect.test_golden_chain_scenario_r2_effect import (
    SETTINGS as R2_SETTINGS,
)
from tests.nodes.node_delegation_scenario_r2_effect.test_golden_chain_scenario_r2_effect import (
    ScriptedOnex,
)

from .support import LAB_NODE_RT as RT

pytestmark = pytest.mark.unit

READ_AT = R2_RUN_AT + timedelta(minutes=10)
DAYS_NODE = "node_delegation_scenario_days_effect"
# The scenario nodes the board can read from the bus: the node, and the scenario id its contract declares.
LAB_NODES = {
    "R2": "node_delegation_scenario_r2_effect",
    "R4": "node_delegation_scenario_r4_effect",
    "R6": "node_delegation_scenario_r6_effect",
}


def contract_runtime(node: str) -> dict[str, Any]:
    text = (
        files(f"omnimarket.nodes.{node}").joinpath("contract.yaml").read_text("utf-8")
    )
    return yaml.safe_load(text)["runtime"]


def days_table() -> dict[str, str]:
    """Scenario id -> the lane node_delegation_scenario_days_effect reads that scenario's rows under."""
    return {
        s["scenario_id"]: s["lane"] for s in contract_runtime(DAYS_NODE)["scenarios"]
    }


class LaneLedger:
    """A ledger holding rows under their lanes; answers a rows query only with the rows of the lanes it names."""

    def __init__(self, *entries: tuple[str, str]) -> None:
        self.entries = list(entries)
        self.queries: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.queries.append(query)
        return ModelPointAnswer(
            lines=tuple(
                row for lane, row in self.entries if lane in (query.lanes or ())
            )
        )


def decl(scenario: str, lane: str) -> ModelScenarioDecl:
    return ModelScenarioDecl(
        id=scenario,
        title=f"scenario {scenario}",
        source="lab_node",
        lane=lane,
        cadence_hours=1,
    )


def run_r2(
    home: Path, brk: str | None = None, lane: str | None = None
) -> tuple[str, str, list[str]]:
    """One real R2 run: the lane it wrote under, the row, and the events it emitted."""
    rows: list[str] = []
    sink = InMemoryScenarioEventSink()
    runtime = (
        R2_RUNTIME if lane is None else R2_RUNTIME.model_copy(update={"lane": lane})
    )
    HandlerScenarioR2(
        runtime,
        home=home,
        environment=R2_SETTINGS,
        runner=ScriptedOnex(brk),
        appender=rows.append,
        sink=sink,
        trigger=EnumScenarioTrigger.SCHEDULE,
        clock=lambda: R2_RUN_AT,
    ).run()
    assert len(rows) == 1
    return runtime.lane, rows[0], [topic for topic, _ in sink.events]


def read_r2(ledger: LaneLedger) -> ModelScenarioRead:
    r2 = next(d for d in RT.scenarios if d.id == "R2")
    return read_lab_node_scenario(r2, ledger, RT, READ_AT)


def test_seam_the_overlay_lane_of_r2_is_the_lane_its_node_writes_under() -> None:
    r2 = next(d for d in RT.scenarios if d.id == "R2")
    assert r2.lane == contract_runtime(LAB_NODES["R2"])["lane"]
    assert r2.lane == R2_RUNTIME.lane


def test_golden_seam_a_row_the_r2_node_wrote_reads_as_the_boards_result(
    tmp_path: Path,
) -> None:
    lane, row, topics = run_r2(tmp_path)
    assert topics, "the node emitted its result on the bus"
    read = read_r2(LaneLedger((lane, row)))
    assert read.read == "read"
    assert [(r.event, r.status, r.conclusion) for r in read.runs] == [
        ("schedule", "completed", "success")
    ]


def test_error_seam_a_failing_r2_run_reads_as_a_failed_result_on_the_board(
    tmp_path: Path,
) -> None:
    lane, row, _ = run_r2(tmp_path, "dead_url_accepted")
    read = read_r2(LaneLedger((lane, row)))
    assert read.read == "read"
    assert [r.conclusion for r in read.runs] == ["failure"]


def test_control_seam_a_node_whose_lane_moved_is_not_read_as_a_pass(
    tmp_path: Path,
) -> None:
    """The injected break: the node writes under another lane than the overlay names. The board must not read a result."""
    lane, row, _ = run_r2(tmp_path, lane="node_delegation_scenario_r2_renamed")
    assert lane != RT.scenarios[1].lane
    read = read_r2(LaneLedger((lane, row)))
    assert read.read == "read"
    assert read.runs == ()


def test_control_seam_a_row_with_a_renamed_cell_is_not_read_as_a_pass(
    tmp_path: Path,
) -> None:
    lane, row, _ = run_r2(tmp_path)
    broken = row.replace(" | conclusion=", " | outcome=")
    assert broken != row
    assert read_r2(LaneLedger((lane, broken))).runs == ()


def lane_problems(scenario: str, table: dict[str, str]) -> list[str]:
    """What differs between the lane the scenario node's contract writes under and the green days table's."""
    runtime = contract_runtime(LAB_NODES[scenario])
    problems = []
    if runtime["scenario_id"] != scenario:
        problems.append(f"{LAB_NODES[scenario]} declares {runtime['scenario_id']}")
    if table.get(scenario) != runtime["lane"]:
        problems.append(
            f"{scenario}: table {table.get(scenario)!r} vs node {runtime['lane']!r}"
        )
    return problems


@pytest.mark.parametrize("scenario", sorted(LAB_NODES))
def test_seam_the_days_reader_and_the_scenario_node_name_one_lane(
    scenario: str,
) -> None:
    assert lane_problems(scenario, days_table()) == []


def test_control_seam_a_lane_changed_in_one_table_is_found() -> None:
    table = dict(days_table())
    table["R2"] = "node_delegation_scenario_r2_renamed"
    assert lane_problems("R2", table) != []
    assert lane_problems("R4", table) == []


@pytest.mark.parametrize("scenario", sorted(LAB_NODES))
def test_seam_a_row_of_each_lab_node_scenario_reads_as_a_pass_under_its_contract_lane(
    scenario: str,
) -> None:
    lane = contract_runtime(LAB_NODES[scenario])["lane"]
    result = ModelScenarioResult(
        scenario_id=scenario,
        slot_start=R2_RUN_AT.replace(minute=0, second=0),
        trigger=EnumScenarioTrigger.SCHEDULE,
        conclusion=EnumScenarioConclusion.SUCCESS,
        recorded_at=R2_RUN_AT,
        assertions=("+a",),
    )
    row = result.to_status_row(lane=lane, ticket="OMN-23407", actor="script:test")
    read = read_lab_node_scenario(
        decl(scenario, lane), LaneLedger((lane, row)), RT, READ_AT
    )
    assert [r.conclusion for r in read.runs] == ["success"]
    # the same row under another scenario's declaration is never its result
    other = "R4" if scenario != "R4" else "R6"
    assert (
        read_lab_node_scenario(
            decl(other, lane), LaneLedger((lane, row)), RT, READ_AT
        ).runs
        == ()
    )


def test_seam_the_reader_asks_for_status_rows_only_under_the_declared_lane() -> None:
    ledger = LaneLedger()
    read_r2(ledger)
    assert ledger.queries
    assert all(q.lanes == (RT.scenarios[1].lane,) for q in ledger.queries)
    assert all(q.kinds == ("STATUS",) for q in ledger.queries)


def test_seam_the_read_window_is_inside_the_windows_of_the_green_days_reader() -> None:
    """The board reads a window the green days reader also covers, so a row one of them sees the other sees."""
    days_window = contract_runtime(DAYS_NODE)["window_days"]
    assert 1 <= RT.lab_node_window_days <= days_window + 1
