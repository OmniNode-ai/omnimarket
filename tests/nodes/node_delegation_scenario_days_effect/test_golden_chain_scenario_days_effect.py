# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the scenario green-days reading (OMN-23171): one run publishes keyed rows, one STATUS row, one event.

The read is a paged ``rows`` query. The error chains are named breaks of that read: a page that does not answer and a read cut at
``max_pages`` both make every row red, and a result row of another scenario or a malformed row changes nothing.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.delegation_scenario_runtime import InMemoryScenarioEventSink
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    ModelScenarioDaysRunCommand,
    ModelScenarioResult,
)
from omnimarket.nodes.node_delegation_scenario_days_effect.handlers.handler_scenario_days_run import (
    HandlerScenarioDaysRun,
    ModelScenarioDaysRuntime,
    build_runner,
    load_runtime,
    load_schedule,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    ModelPointAnswer,
    ModelPointQuery,
)

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.delegation-scenario-green-days-published.v1"
NOW = datetime(2026, 11, 9, 12, 20, tzinfo=UTC)
RUNTIME = load_runtime()
LANE = {s.scenario_id: s.lane for s in RUNTIME.scenarios}["R2"]


def _row(slot: datetime, conclusion: str = "success", trigger: str = "schedule") -> str:
    return ModelScenarioResult(
        scenario_id="R2",
        slot_start=slot,
        trigger=EnumScenarioTrigger(trigger),
        conclusion=EnumScenarioConclusion(conclusion),
        recorded_at=slot + timedelta(minutes=5),
    ).to_status_row(lane=LANE, ticket="OMN-23172", actor="script:test")


def _days(count: int) -> list[str]:
    midnight = NOW.replace(hour=0, minute=0)
    return [
        _row(midnight - timedelta(days=offset) + timedelta(hours=hour))
        for offset in range(1, count + 1)
        for hour in range(24)
    ]


class PagedLedger:
    """A scripted bus read: the rows split into pages of ``size``; page ``fail_on`` (1-based) answers with an error."""

    def __init__(
        self, rows: list[str], size: int = 100, fail_on: int | None = None
    ) -> None:
        self.rows, self.size, self.fail_on = rows, size, fail_on
        self.requests: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.requests.append(query)
        page = len(self.requests)
        if page == self.fail_on:
            return ModelPointAnswer(error="read-command-exit-1")
        start = int(query.cursor or 0)
        chunk = self.rows[start : start + self.size]
        more = start + self.size < len(self.rows)
        return ModelPointAnswer(
            lines=tuple(chunk),
            matched=len(chunk),
            next_cursor=str(start + self.size) if more else None,
        )


def _handler(
    tmp_path: Path, ledger: PagedLedger, runtime: ModelScenarioDaysRuntime = RUNTIME
) -> tuple[HandlerScenarioDaysRun, list[str], InMemoryScenarioEventSink]:
    rows: list[str] = []
    sink = InMemoryScenarioEventSink()
    handler = HandlerScenarioDaysRun(
        runtime,
        home=tmp_path,
        ledger=ledger,
        appender=rows.append,
        sink=sink,
        clock=lambda: NOW,
    )
    return handler, rows, sink


def test_golden_chain_scenario_days_effect_declares_its_runtime_and_schedule() -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_delegation_scenario_days_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    assert contract["terminal_event"] == TERMINAL
    assert [s["scenario_id"] for s in contract["runtime"]["scenarios"]] == [
        f"R{n}" for n in range(1, 10)
    ]
    assert load_schedule().calendar == "*-*-* *:20:00 UTC"


def test_golden_chain_scenario_days_effect_publishes_keyed_rows_one_status_row_and_one_event(
    tmp_path: Path,
) -> None:
    ledger = PagedLedger(_days(5), size=50)
    handler, rows, sink = _handler(tmp_path, ledger)
    result = handler.handle(ModelScenarioDaysRunCommand())
    assert result.by_id()["R2"].green_days == 5
    assert result.by_id()["R1"].green_days == 0  # declared, nothing recorded: not green
    published = json.loads(handler.published_path.read_text(encoding="utf-8"))
    assert sorted(published["rows"]) == [f"R{n}" for n in range(1, 10)]
    assert published["rows"]["R2"]["green_days"] == 5
    assert len(rows) == 1
    assert (
        " | STATUS | lane=node_delegation_scenario_days_effect | ticket=OMN-23171 | "
        in rows[0]
    )
    assert "green_days=R1:0,R2:5," in rows[0]
    assert "unreadable=none" in rows[0]
    assert [topic for topic, _ in sink.events] == [TERMINAL]
    assert len(ledger.requests) == 3
    assert ledger.requests[0].kinds == ("STATUS",)
    assert ledger.requests[0].since == NOW - timedelta(days=RUNTIME.window_days + 1)


def test_golden_chain_scenario_days_effect_a_rerun_rewrites_the_same_rows(
    tmp_path: Path,
) -> None:
    handler, _, _ = _handler(tmp_path, PagedLedger(_days(3)))
    handler.run()
    first = handler.published_path.read_text(encoding="utf-8")
    handler.run()
    assert handler.published_path.read_text(encoding="utf-8") == first


def test_error_chain_scenario_days_effect_a_page_that_does_not_answer_makes_every_row_red(
    tmp_path: Path,
) -> None:
    ledger = PagedLedger(_days(5), size=50, fail_on=2)
    handler, rows, sink = _handler(tmp_path, ledger)
    result = handler.run()
    assert all(row.red and not row.readable for row in result.rows)
    assert "page 2 did not answer" in result.by_id()["R2"].reason
    assert "unreadable=R1,R2,R3,R4,R5,R6,R7,R8,R9" in rows[0]
    published_rows = sink.events[0][1]["rows"]
    assert isinstance(published_rows, list)
    assert published_rows[1]["scenario_id"] == "R2"
    # Without the break the same ledger reads five green days.
    clean, _, _ = _handler(tmp_path / "clean", PagedLedger(_days(5), size=50))
    assert clean.run().by_id()["R2"].green_days == 5


def test_error_chain_scenario_days_effect_a_read_cut_at_max_pages_is_red(
    tmp_path: Path,
) -> None:
    runtime = RUNTIME.model_copy(update={"max_pages": 2})
    handler, _, _ = _handler(tmp_path, PagedLedger(_days(5), size=50), runtime)
    row = handler.run().by_id()["R2"]
    assert (row.readable, row.red, row.green_days) == (False, True, 0)
    assert "stopped after 2 pages" in row.reason


def test_error_chain_scenario_days_effect_foreign_and_malformed_rows_change_nothing(
    tmp_path: Path,
) -> None:
    noise = [
        "not a row at all",
        "2026-11-08T01:00:00Z | STATUS | lane=other | ticket=OMN-1 | scenario=R2 | slot=bad | trigger=schedule | conclusion=success | x",
        "2026-11-08T01:00:00Z | STATUS | lane=other | ticket=OMN-1 | note=no scenario cells | x",
        _row(NOW - timedelta(days=1), "success").replace("scenario=R2", "scenario=R12"),
    ]
    handler, _, _ = _handler(tmp_path, PagedLedger(noise + _days(2)))
    assert handler.run().by_id()["R2"].green_days == 2


def test_error_chain_scenario_days_effect_with_no_read_route_refuses_to_build(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match=RUNTIME.ledger_read_command_env):
        build_runner(
            {RUNTIME.home_env: str(tmp_path), RUNTIME.ledger_append_command_env: "true"}
        )


def test_golden_chain_scenario_days_effect_runs_through_the_bus_commands_the_environment_names(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake_bus.py"
    script.write_text(
        "import json, sys\n"
        "mode = sys.argv[1]\n"
        "if mode == 'read':\n"
        "    request = json.loads(sys.stdin.read())\n"
        "    assert request['query'] == 'rows' and request['kinds'] == ['STATUS']\n"
        "    print(json.dumps({'correlation_id': request['correlation_id'], 'status': 'ok', 'lines': [], 'matched': 0}))\n"
        "elif mode == 'append':\n"
        "    open(sys.argv[2], 'a').write(sys.stdin.read())\n"
        "    print(json.dumps({'status': 'accepted', 'message': 'ok'}))\n"
        "else:\n"
        "    open(sys.argv[2], 'a').write(sys.argv[3] + '\\n' + sys.stdin.read())\n",
        encoding="utf-8",
    )
    appended, emitted = tmp_path / "appended.txt", tmp_path / "emitted.txt"
    runner = build_runner(
        {
            RUNTIME.home_env: str(tmp_path / "registry"),
            RUNTIME.ledger_read_command_env: f"{sys.executable} {script} read",
            RUNTIME.ledger_append_command_env: f"{sys.executable} {script} append {appended}",
            RUNTIME.bus_emit_command_env: f"{sys.executable} {script} emit {emitted}",
            RUNTIME.host_env: "lab-host",
        }
    )
    result = runner.run()
    assert all(row.readable and row.green_days == 0 for row in result.rows)
    assert (
        " | STATUS | lane=node_delegation_scenario_days_effect | ticket=OMN-23171 | "
        in appended.read_text(encoding="utf-8")
    )
    assert emitted.read_text(encoding="utf-8").startswith(TERMINAL)
