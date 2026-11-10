# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the readiness verdict (OMN-23181): one run publishes the verdict, one STATUS row, one event.

The bar is seven consecutive green UTC days for each of the nine scenarios, no failed slot today and a successful run in
the latest due slot. Each red test is a named break of one clause of that rule: the PR text lists, per test, the edit that
turns it green again.

The clock stands at 12:35 UTC; the contract's grace puts the latest due slot at 10:00 (hourly), 00:00 (every 6 h) and
yesterday (daily).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.delegation_scenario_runtime import InMemoryScenarioEventSink
from omnimarket.models.model_delegation_readiness_verdict import (
    EnumReadinessVerdict,
    ModelReadinessVerdictRunCommand,
)
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioCadence,
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    ModelScenarioResult,
)
from omnimarket.nodes.node_delegation_readiness_verdict_effect.handlers.handler_readiness_verdict_run import (
    HandlerReadinessVerdictRun,
    ModelReadinessVerdictRuntime,
    build_runner,
    load_runtime,
    load_schedule,
)
from omnimarket.nodes.node_delegation_readiness_verdict_effect.runtime_verdict import (
    main,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    ModelPointAnswer,
    ModelPointQuery,
)

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.delegation-readiness-verdict-published.v1"
NOW = datetime(2026, 11, 9, 12, 35, tzinfo=UTC)
MIDNIGHT = NOW.replace(hour=0, minute=0)
RUNTIME = load_runtime()
SCENARIOS = {s.scenario_id: s for s in RUNTIME.scenarios}
ALL = tuple(SCENARIOS)


def _slots(
    cadence: EnumScenarioCadence, first_day: int, last_slot: datetime
) -> list[datetime]:
    """Every slot start from ``first_day`` days before today's midnight through ``last_slot`` (inclusive)."""
    slots: list[datetime] = []
    moment = MIDNIGHT - timedelta(days=first_day)
    while moment <= last_slot:
        slots.append(moment)
        moment += timedelta(hours=cadence.slot_hours)
    return slots


def _row(
    scenario_id: str,
    slot: datetime,
    conclusion: str = "success",
    trigger: str = "schedule",
) -> str:
    return ModelScenarioResult(
        scenario_id=scenario_id,
        slot_start=slot,
        trigger=EnumScenarioTrigger(trigger),
        conclusion=EnumScenarioConclusion(conclusion),
        recorded_at=slot + timedelta(minutes=5),
    ).to_status_row(
        lane=SCENARIOS[scenario_id].lane, ticket="OMN-23181", actor="script:test"
    )


def _scenario_rows(
    scenario_id: str,
    days: int = 7,
    through: datetime | None = None,
    skip: tuple[datetime, ...] = (),
) -> list[str]:
    """``days`` full green UTC days ending yesterday, then today's slots through the last one that ran by ``NOW``."""
    cadence = SCENARIOS[scenario_id].cadence
    last = through or (NOW - timedelta(minutes=5))
    return [
        _row(scenario_id, slot)
        for slot in _slots(cadence, days, last)
        if slot not in skip and slot + timedelta(minutes=5) <= NOW
    ]


def _all_rows(**overrides: list[str]) -> list[str]:
    """Seven green days for every scenario, with the rows of the named scenarios replaced."""
    rows: list[str] = []
    for scenario_id in ALL:
        rows.extend(overrides.get(scenario_id, _scenario_rows(scenario_id)))
    return rows


class PagedLedger:
    """A scripted bus read: the rows split into pages of ``size``; page ``fail_on`` (1-based) answers with an error."""

    def __init__(
        self, rows: list[str], size: int = 200, fail_on: int | None = None
    ) -> None:
        self.rows, self.size, self.fail_on = rows, size, fail_on
        self.requests: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.requests.append(query)
        if len(self.requests) == self.fail_on:
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
    tmp_path: Path,
    ledger: PagedLedger,
    runtime: ModelReadinessVerdictRuntime = RUNTIME,
) -> tuple[HandlerReadinessVerdictRun, list[str], InMemoryScenarioEventSink]:
    rows: list[str] = []
    sink = InMemoryScenarioEventSink()
    handler = HandlerReadinessVerdictRun(
        runtime,
        home=tmp_path,
        ledger=ledger,
        appender=rows.append,
        sink=sink,
        clock=lambda: NOW,
    )
    return handler, rows, sink


def test_golden_chain_readiness_verdict_effect_declares_the_bar_the_grace_and_the_schedule() -> (
    None
):
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_delegation_readiness_verdict_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    assert contract["terminal_event"] == TERMINAL
    runtime = contract["runtime"]
    assert runtime["required_green_days"] == 7
    assert runtime["window_days"] == 8
    assert runtime["due_grace_hours"] == {"hourly": 2, "every_6h": 8, "daily": 26}
    assert [s["scenario_id"] for s in runtime["scenarios"]] == [
        f"R{n}" for n in range(1, 10)
    ]
    assert load_schedule().calendar == "*-*-* *:35:00 UTC"


def test_golden_chain_all_seven_green_days_passes_with_one_row_one_event_and_a_published_verdict(
    tmp_path: Path,
) -> None:
    ledger = PagedLedger(_all_rows())
    handler, rows, sink = _handler(tmp_path, ledger)
    verdict = handler.handle(ModelReadinessVerdictRunCommand())
    assert verdict.verdict is EnumReadinessVerdict.PASS
    assert verdict.misses == ()
    assert all(row.green_days >= 7 and not row.red for row in verdict.rows)
    assert all(row.latest_due_slot_ok and not row.failed_today for row in verdict.rows)
    published = json.loads(handler.published_path.read_text(encoding="utf-8"))
    assert published["verdict"] == "pass"
    assert sorted(published["rows"]) == list(ALL)
    assert len(rows) == 1
    assert (
        " | STATUS | lane=node_delegation_readiness_verdict_effect | ticket=OMN-23181 | "
        in rows[0]
    )
    assert "verdict=pass" in rows[0]
    assert "misses=none" in rows[0]
    assert [topic for topic, _ in sink.events] == [TERMINAL]
    assert sink.events[0][1]["verdict"] == "pass"
    assert ledger.requests[0].kinds == ("STATUS",)
    assert ledger.requests[0].since == NOW - timedelta(days=RUNTIME.window_days + 1)


def test_golden_chain_all_seven_a_slot_still_inside_its_grace_is_not_due(
    tmp_path: Path,
) -> None:
    """Today's rows stop at each cadence's latest due slot (hourly 10:00, every 6 h 00:00): the later slots are not yet due."""
    due = {
        "hourly": MIDNIGHT + timedelta(hours=10),
        "every_6h": MIDNIGHT,
        "daily": MIDNIGHT - timedelta(days=1),
    }
    rows: list[str] = []
    for scenario_id in ALL:
        cadence = SCENARIOS[scenario_id].cadence
        rows.extend(_scenario_rows(scenario_id, through=due[cadence.value]))
    handler, _, _ = _handler(tmp_path, PagedLedger(rows))
    assert handler.run().verdict is EnumReadinessVerdict.PASS


def test_golden_chain_a_rerun_rewrites_the_same_verdict(tmp_path: Path) -> None:
    handler, _, _ = _handler(tmp_path, PagedLedger(_all_rows()))
    handler.run()
    first = handler.published_path.read_text(encoding="utf-8")
    handler.run()
    assert handler.published_path.read_text(encoding="utf-8") == first


def test_error_chain_one_short_scenario_with_six_green_days_is_red_and_named(
    tmp_path: Path,
) -> None:
    handler, rows, sink = _handler(
        tmp_path, PagedLedger(_all_rows(R3=_scenario_rows("R3", days=6)))
    )
    verdict = handler.run()
    assert verdict.verdict is EnumReadinessVerdict.FAIL
    assert verdict.misses == ("R3:green_days 6 of 7",)
    assert [row.scenario_id for row in verdict.rows if row.red] == ["R3"]
    assert verdict.by_id()["R3"].green_days == 6
    assert "verdict=fail" in rows[0]
    assert "misses=R3:green_days 6 of 7 |" in rows[0]
    assert sink.events[0][1]["verdict"] == "fail"


def test_error_chain_failed_today_makes_the_scenario_red_even_with_seven_green_days(
    tmp_path: Path,
) -> None:
    broken = _scenario_rows("R2", skip=(MIDNIGHT + timedelta(hours=9),))
    broken.append(_row("R2", MIDNIGHT + timedelta(hours=9), "failure"))
    handler, _, _ = _handler(tmp_path, PagedLedger(_all_rows(R2=broken)))
    verdict = handler.run()
    row = verdict.by_id()["R2"]
    assert row.failed_today
    assert row.green_days >= 7
    assert row.latest_due_slot_ok
    assert verdict.misses == ("R2:failed_today",)
    assert verdict.verdict is EnumReadinessVerdict.FAIL


def test_error_chain_stopped_after_qualifying_runs_stop_past_the_grace_is_red(
    tmp_path: Path,
) -> None:
    """R5 (hourly) has seven green days and ran through 08:00 today, then nothing: the 10:00 slot is due and empty."""
    stopped = _scenario_rows("R5", through=MIDNIGHT + timedelta(hours=8))
    handler, _, _ = _handler(tmp_path, PagedLedger(_all_rows(R5=stopped)))
    verdict = handler.run()
    row = verdict.by_id()["R5"]
    assert row.green_days >= 7
    assert not row.failed_today
    assert row.latest_due_slot == MIDNIGHT + timedelta(hours=10)
    assert row.latest_due_slot_ok is False
    assert verdict.misses == ("R5:due_slot 2026-11-09T10:00Z has no scheduled run",)
    assert verdict.verdict is EnumReadinessVerdict.FAIL


def test_error_chain_a_due_slot_whose_run_failed_is_red(tmp_path: Path) -> None:
    due = MIDNIGHT + timedelta(hours=10)
    failed = [*_scenario_rows("R1", skip=(due,)), _row("R1", due, "failure")]
    handler, _, _ = _handler(tmp_path, PagedLedger(_all_rows(R1=failed)))
    row = handler.run().by_id()["R1"]
    assert row.red
    assert row.failed_today
    assert row.latest_due_slot_ok is False


def test_error_chain_a_manual_run_never_fills_a_due_slot(tmp_path: Path) -> None:
    """R4 (every 6 h): the 00:00 slot is due and only a manual run is recorded for it."""
    due = MIDNIGHT
    manual = [
        *_scenario_rows("R4", skip=(due,)),
        _row("R4", due, trigger="manual"),
    ]
    handler, _, _ = _handler(tmp_path, PagedLedger(_all_rows(R4=manual)))
    verdict = handler.run()
    assert verdict.by_id()["R4"].latest_due_slot_ok is False
    assert verdict.misses == ("R4:due_slot 2026-11-09T00:00Z has no scheduled run",)


def test_error_chain_a_scenario_with_no_rows_is_red(tmp_path: Path) -> None:
    handler, _, _ = _handler(tmp_path, PagedLedger(_all_rows(R7=[])))
    verdict = handler.run()
    assert verdict.by_id()["R7"].red
    assert verdict.by_id()["R7"].misses[0] == "no_rows"
    assert [row.scenario_id for row in verdict.rows if row.red] == ["R7"]


def test_error_chain_unreadable_is_red_a_page_that_does_not_answer_fails_every_scenario(
    tmp_path: Path,
) -> None:
    ledger = PagedLedger(_all_rows(), size=100, fail_on=2)
    handler, rows, _ = _handler(tmp_path, ledger)
    verdict = handler.run()
    assert verdict.verdict is EnumReadinessVerdict.FAIL
    assert all(row.red and row.misses == ("unreadable_is_red",) for row in verdict.rows)
    assert len(verdict.misses) == 9
    assert "verdict=fail" in rows[0]
    # Without the break the same ledger passes.
    clean, _, _ = _handler(tmp_path / "clean", PagedLedger(_all_rows(), size=100))
    assert clean.run().verdict is EnumReadinessVerdict.PASS


def test_error_chain_unreadable_is_red_a_read_cut_at_max_pages_fails_every_scenario(
    tmp_path: Path,
) -> None:
    runtime = RUNTIME.model_copy(update={"max_pages": 2})
    handler, _, _ = _handler(tmp_path, PagedLedger(_all_rows(), size=100), runtime)
    verdict = handler.run()
    assert verdict.verdict is EnumReadinessVerdict.FAIL
    assert all(row.misses == ("unreadable_is_red",) for row in verdict.rows)


def test_error_chain_the_bar_is_the_contract_setting_not_a_constant(
    tmp_path: Path,
) -> None:
    rows = _all_rows(R3=_scenario_rows("R3", days=6))
    lower = RUNTIME.model_copy(update={"required_green_days": 6})
    handler, _, _ = _handler(tmp_path, PagedLedger(rows), lower)
    assert handler.run().verdict is EnumReadinessVerdict.PASS


def test_error_chain_a_ledger_append_that_fails_raises_and_emits_nothing(
    tmp_path: Path,
) -> None:
    def refuse(row: str) -> None:
        raise RuntimeError("the ledger append was rejected")

    sink = InMemoryScenarioEventSink()
    handler = HandlerReadinessVerdictRun(
        RUNTIME,
        home=tmp_path,
        ledger=PagedLedger(_all_rows()),
        appender=refuse,
        sink=sink,
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="ledger append"):
        handler.run()
    assert sink.events == []


def test_error_chain_a_bus_emit_that_fails_raises_after_the_row_is_written(
    tmp_path: Path,
) -> None:
    class Down:
        def emit(self, topic: str, payload: Mapping[str, object]) -> None:
            raise RuntimeError("the bus emit failed")

    rows: list[str] = []
    handler = HandlerReadinessVerdictRun(
        RUNTIME,
        home=tmp_path,
        ledger=PagedLedger(_all_rows()),
        appender=rows.append,
        sink=Down(),
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="bus emit"):
        handler.run()
    assert len(rows) == 1


def test_cli_exit_zero_on_pass_and_prints_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    handler, _, _ = _handler(tmp_path, PagedLedger(_all_rows()))
    assert main(["run"], runner_factory=lambda: handler) == 0
    out = capsys.readouterr().out
    assert out.startswith("PASS")
    assert "R3" not in out


def test_cli_exit_nonzero_names_each_missing_scenario(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = _all_rows(
        R3=_scenario_rows("R3", days=2),
        R8=_scenario_rows("R8", through=MIDNIGHT - timedelta(hours=1), days=7),
    )
    handler, _, _ = _handler(tmp_path, PagedLedger(rows))
    assert main(["run"], runner_factory=lambda: handler) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("R3: green_days 2 of 7")
    assert lines[1].startswith("R8: ")
    assert lines[-1].startswith("FAIL: 2 of 9")
    assert not any(
        line.startswith(("R1", "R2", "R4", "R5", "R6", "R7", "R9")) for line in lines
    )


def test_error_chain_with_no_read_route_refuses_to_build(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=RUNTIME.ledger_read_command_env):
        build_runner(
            {RUNTIME.home_env: str(tmp_path), RUNTIME.ledger_append_command_env: "true"}
        )


def test_golden_chain_runs_through_the_bus_commands_the_environment_names(
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
    verdict = runner.run()
    assert (
        verdict.verdict is EnumReadinessVerdict.FAIL
    )  # an empty ledger is never ready
    assert all("no_rows" in row.misses for row in verdict.rows)
    assert (
        " | STATUS | lane=node_delegation_readiness_verdict_effect | ticket=OMN-23181 | "
        in appended.read_text(encoding="utf-8")
    )
    assert "verdict=fail" in appended.read_text(encoding="utf-8")
    assert emitted.read_text(encoding="utf-8").startswith(TERMINAL)
