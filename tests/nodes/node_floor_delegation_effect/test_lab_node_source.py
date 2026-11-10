# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A scenario read from a lab node's latest scheduled result on the bus (OMN-23407), chosen per scenario by the overlay.

The failing control is the first test: a scenario whose overlay entry names the lab-node source and whose node has a passing
scheduled result on the bus reads ``pass``. On the reader that knows only hosted workflows it reads "Not built yet".
Each error is a named injected break: a read that does not answer, a read cut short, a manual run, a missing read route and a
result older than the read limit are never a pass.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.model_point_query import ModelPointQuery
from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_sources import (
    read_lab_node_scenario,
)
from omnimarket.nodes.node_floor_delegation_effect.handlers.handler_floor_delegation import (
    HandlerFloorDelegation,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelFloorDelegationRequest,
    ModelScenarioDecl,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import ModelPointAnswer

from .support import LAB_NODE_RT as RT
from .support import NOW, FakeRunner, iso, runs_answer, write_home

pytestmark = pytest.mark.unit

R2 = next(d for d in RT.scenarios if d.id == "R2")


def status_row(
    conclusion: str = "success",
    minutes_ago: int = 10,
    *,
    trigger: str = "schedule",
    scenario: str = "R2",
    lane: str = R2.lane,
) -> str:
    at = NOW - timedelta(minutes=minutes_ago)
    slot = at.replace(minute=0, second=0, microsecond=0)
    return (
        f"{iso(at)} | STATUS | lane={lane} | ticket=OMN-23172 | actor=script:x | model=none | "
        f"scenario={scenario} | slot={iso(slot)} | trigger={trigger} | conclusion={conclusion} | "
        f"assertions=+a,+b | scenario {scenario} {conclusion}"
    )


class FakeLedger:
    """Answers each page from a list; records every query."""

    def __init__(self, *pages: ModelPointAnswer) -> None:
        self.pages = list(pages)
        self.queries: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.queries.append(query)
        return self.pages.pop(0) if self.pages else ModelPointAnswer(lines=())


def page(*rows: str, cursor: str | None = None) -> ModelPointAnswer:
    return ModelPointAnswer(lines=tuple(rows), next_cursor=cursor)


def test_control_a_passing_lab_node_result_reads_pass_not_not_built(
    tmp_path: Path,
) -> None:
    """The failing control (AC1): the overlay's R2 names the lab node, its node has a passing scheduled result on the bus."""
    home = write_home(tmp_path)
    ledger = FakeLedger(page(status_row("success", 10)))
    green = FakeRunner(
        {
            d.workflow: (0, runs_answer(("completed", "success", 10)), "")
            for d in RT.scenarios
            if d.source == "workflow"
        }
    )
    handler = HandlerFloorDelegation(RT, green, clock=lambda: NOW, ledger=ledger)
    assert handler.handle(
        ModelFloorDelegationRequest(stage="scenarios", home=str(home))
    ).ok
    assert handler.handle(ModelFloorDelegationRequest(stage="build", home=str(home))).ok
    doc = json.loads((home / RT.output_relpath).read_text())
    r2 = next(s for s in doc["scenarios"] if s["id"] == "R2")
    assert r2["state"] == "pass"
    assert r2["source"] == "lab_node"
    assert r2["lane"] == R2.lane
    assert r2["workflow_url"] == ""
    assert doc["shippable"] is True
    assert doc["counts"]["scenarios_not_built"] == 0

    assert R2.workflow not in green.read


def test_the_overlay_chooses_the_source_per_scenario() -> None:
    sources = {d.id: d.source for d in RT.scenarios}
    assert sources["R2"] == "lab_node"
    assert {s for i, s in sources.items() if i != "R2"} == {"workflow"}


def test_golden_the_newest_scheduled_result_is_the_scenario_result() -> None:
    ledger = FakeLedger(
        page(
            status_row("success", 130),
            status_row("failure", 70),
            status_row("success", 10),
        )
    )
    got = read_lab_node_scenario(R2, ledger, RT, NOW)
    assert got.read == "read"
    assert [r.conclusion for r in got.runs] == ["success", "failure", "success"]
    assert all(r.event == "schedule" and r.status == "completed" for r in got.runs)
    query = ledger.queries[0]
    assert query.query == "rows"
    assert query.lanes == (R2.lane,)
    assert query.kinds == ("STATUS",)
    assert query.since == NOW - timedelta(days=RT.lab_node_window_days)
    assert query.page_size == RT.lab_node_rows_page_size


def test_golden_pages_are_followed_to_the_end() -> None:
    ledger = FakeLedger(
        page(status_row("success", 130), cursor="c1"),
        page(status_row("success", 10)),
    )
    got = read_lab_node_scenario(R2, ledger, RT, NOW)
    assert got.read == "read"
    assert len(got.runs) == 2
    assert ledger.queries[1].cursor == "c1"


def test_a_failing_latest_result_reads_fail() -> None:
    ledger = FakeLedger(page(status_row("success", 70), status_row("failure", 10)))
    got = read_lab_node_scenario(R2, ledger, RT, NOW)
    assert got.runs[0].conclusion == "failure"


def test_error_a_manual_run_or_another_scenarios_row_is_never_the_result() -> None:
    ledger = FakeLedger(
        page(
            status_row("success", 5, trigger="manual"),
            status_row("success", 5, scenario="R4"),
            "not a ledger row",
            status_row("failure", 20),
        )
    )
    got = read_lab_node_scenario(R2, ledger, RT, NOW)
    assert [r.conclusion for r in got.runs] == ["failure"]


def test_error_a_rerun_of_a_slot_keeps_its_newest_row() -> None:
    ledger = FakeLedger(page(status_row("failure", 20), status_row("success", 10)))
    got = read_lab_node_scenario(R2, ledger, RT, NOW)
    assert [r.conclusion for r in got.runs] == ["success"]


def test_error_a_page_that_does_not_answer_is_unread_never_a_pass() -> None:
    ledger = FakeLedger(
        page(status_row("success", 10), cursor="c1"),
        ModelPointAnswer(error="read-command-exit-1"),
    )
    got = read_lab_node_scenario(R2, ledger, RT, NOW)
    assert got.read == "unread"
    assert "read-command-exit-1" in got.reason
    assert got.runs == ()


def test_error_a_read_cut_at_the_page_limit_is_unread() -> None:
    rt = RT.model_copy(update={"lab_node_max_pages": 2})
    ledger = FakeLedger(
        page(status_row("success", 10), cursor="c1"),
        page(status_row("success", 20), cursor="c2"),
    )
    got = read_lab_node_scenario(R2, ledger, rt, NOW)
    assert got.read == "unread"
    assert "2 pages" in got.reason


def test_error_no_result_in_the_window_is_no_run_not_a_pass(tmp_path: Path) -> None:
    home = write_home(tmp_path)
    handler = HandlerFloorDelegation(
        RT, FakeRunner({}), clock=lambda: NOW, ledger=FakeLedger(page())
    )
    handler.handle(ModelFloorDelegationRequest(stage="run", home=str(home)))
    doc = json.loads((home / RT.output_relpath).read_text())
    r2 = next(s for s in doc["scenarios"] if s["id"] == "R2")
    assert r2["state"] == "no_run"
    assert "lab node" in r2["reason"]
    assert doc["shippable"] is False


def test_error_without_a_read_route_the_lab_node_scenario_is_unread_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(RT.ledger_read_command_env, raising=False)
    home = write_home(tmp_path)
    handler = HandlerFloorDelegation(RT, FakeRunner({}), clock=lambda: NOW)
    handler.handle(ModelFloorDelegationRequest(stage="run", home=str(home)))
    doc = json.loads((home / RT.output_relpath).read_text())
    r2 = next(s for s in doc["scenarios"] if s["id"] == "R2")
    assert r2["state"] == "unread"
    assert RT.ledger_read_command_env in r2["reason"]


def test_error_a_result_older_than_the_read_limit_is_not_shown_as_current(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path)
    ledger = FakeLedger(page(status_row("success", 10)))
    HandlerFloorDelegation(RT, FakeRunner({}), clock=lambda: NOW, ledger=ledger).handle(
        ModelFloorDelegationRequest(stage="scenarios", home=str(home))
    )
    later = NOW + timedelta(seconds=RT.scenario_reads_max_age_seconds + 60)
    HandlerFloorDelegation(RT, FakeRunner({}), clock=lambda: later).handle(
        ModelFloorDelegationRequest(stage="build", home=str(home))
    )
    doc = json.loads((home / RT.output_relpath).read_text())
    r2 = next(s for s in doc["scenarios"] if s["id"] == "R2")
    assert r2["state"] == "unread"


def test_error_the_overlay_refuses_a_lab_node_entry_with_no_lane_and_a_workflow_entry_with_no_workflow() -> (
    None
):
    with pytest.raises(ValidationError):
        ModelScenarioDecl(id="R2", title="t", source="lab_node", cadence_hours=1)
    with pytest.raises(ValidationError):
        ModelScenarioDecl(
            id="R1", title="t", repo="o/r", cadence_hours=1
        )  # workflow source, no workflow
    ModelScenarioDecl(id="R2", title="t", source="lab_node", lane="x", cadence_hours=1)


def test_error_a_workflow_scenario_still_reads_through_the_hosted_route(
    tmp_path: Path,
) -> None:
    """The lab-node source changes only the scenarios the overlay names; every other one is read as before."""
    home = write_home(tmp_path)
    runner = FakeRunner(
        {
            d.workflow: (0, runs_answer(("completed", "failure", 10)), "")
            for d in RT.scenarios
            if d.source == "workflow"
        }
    )
    ledger = FakeLedger(page(status_row("success", 10)))
    HandlerFloorDelegation(RT, runner, clock=lambda: NOW, ledger=ledger).handle(
        ModelFloorDelegationRequest(stage="run", home=str(home))
    )
    doc = json.loads((home / RT.output_relpath).read_text())
    states = {s["id"]: s["state"] for s in doc["scenarios"]}
    assert states["R2"] == "pass"
    assert {v for k, v in states.items() if k != "R2"} == {"fail"}
    assert len(runner.read) == 8
