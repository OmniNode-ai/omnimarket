# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed delegation scenario run writes one ledger alarm row (OMN-24874)."""

from __future__ import annotations

import contextlib
import importlib
import inspect
import json
import shlex
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Self
from uuid import UUID

import pytest
import yaml
from click.testing import CliRunner
from omnibase_core.container import ModelONEXContainer
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from omnibase_core.protocols.event_bus.protocol_event_bus_subscriber import (
    ProtocolEventBusSubscriber,
)
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import (
    discover_contracts_from_paths,
)
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.auto_wiring.report import (
    EnumWiringOutcome,
    ModelAutoWiringReport,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.handlers import delegation_readiness_scenario as readiness_scenario
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    ModelScenarioResult,
)
from omnimarket.models.work_ledger_append.model_work_ledger_append import (
    ModelWorkLedgerAppendReceipt,
)
from omnimarket.nodes.node_delegation_scenario_alarm_effect.handlers.handler_delegation_scenario_alarm import (
    CONTRACT_PATH,
    EnumScenarioAlarm,
    HandlerDelegationScenarioAlarm,
    ModelScenarioAlarmRequest,
    load_alarm_settings,
)
from omnimarket.nodes.node_delegation_scenario_alarm_effect.runtime_delegation_scenario_alarm_tick import (
    main,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
    InMemoryLedger,
    stamp_request_cell,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelAppendOutcome,
    ModelLedgerQuery,
)
from omnimarket.nodes.node_runtime_sweep.__main__ import _probe_entry_point
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.adapters import (
    InMemoryBusLedgerStore,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers import LedgerFlipFence
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerFile,
    ModelAppendCommandResult,
)
from omnimarket.work_ledger_bus import cli as bus_cli
from omnimarket.work_ledger_bus.cli import append_command
from tests.nodes.node_host_installer_registry_units.test_registry_units import request

_NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
_REPO = Path(__file__).resolve().parents[3]


class _Appender:
    def __init__(self, status: str = "accepted") -> None:
        self.calls: list[tuple[str, str]] = []
        self._status = status

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.calls.append((rows, request_id))
        return ModelAppendOutcome(status=self._status)


class _Scenario:
    def __init__(self, conclusion: str | None) -> None:
        self._conclusion = conclusion

    def run(self) -> ModelScenarioResult:
        if self._conclusion is None:
            raise RuntimeError("no ledger route")
        return ModelScenarioResult(
            scenario_id="R8",
            slot_start=_NOW,
            trigger=EnumScenarioTrigger.SCHEDULE,
            conclusion=EnumScenarioConclusion(self._conclusion),
            recorded_at=_NOW,
        )


def _record(
    home: Path,
    scenario: str,
    conclusion: str,
    ran_at: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write the scenario's last-run record through the real tick writer (scenario_tick_main), at ``ran_at``.

    ``conclusion`` "refused" is the writer's own refusal record (the scenario could not run).
    """
    moment = datetime.fromisoformat(ran_at)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            del tz
            return cls.combine(moment.date(), moment.time(), moment.tzinfo)

    monkeypatch.setattr(readiness_scenario, "datetime", _Clock)
    state_dir = home / f".local/state/omni/delegation-scenario-{scenario}"
    code = readiness_scenario.scenario_tick_main(
        ["--omni-home", str(home), "--state-dir", str(state_dir)],
        prog=f"scenario-{scenario}",
        scenario_id=scenario.upper(),
        home_env="ONEX_TEST_HOME",
        build=lambda _env, _trigger: _Scenario(
            None if conclusion == "refused" else conclusion
        ),
    )
    assert code == {"success": 0, "failure": 1, "refused": 65}[conclusion]


def _handler(appender: _Appender) -> HandlerDelegationScenarioAlarm:
    return HandlerDelegationScenarioAlarm(
        load_alarm_settings(), appender, clock=lambda: _NOW
    )


@pytest.mark.unit
def test_a_failed_r8_run_writes_one_alarm_row_and_a_passing_r9_writes_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tmp_path, "r8", "failure", "2026-11-16T14:48:29+00:00", monkeypatch)
    _record(tmp_path, "r9", "success", "2026-11-16T14:48:30+00:00", monkeypatch)
    appender = _Appender()
    result = _handler(appender).handle(
        ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    )
    assert len(appender.calls) == 1
    row, request_id = appender.calls[0]
    cells = row.split(" | ")
    # A failed run is stamped with its own time, independently of the tick bucket.
    assert cells[0] == "2026-11-16T14:48:29Z"
    assert cells[1] == "STATUS"
    for cell in (
        "src=alarm",
        "alarm=delegation-scenario-r8-failed",
        "scenario=R8",
        "conclusion=failure",
        "host=h053",
        "unit=onex-delegation-scenario-r8",
    ):
        assert cell in cells
    assert any(cell.startswith("owner=") for cell in cells)
    assert str(UUID(request_id)) == request_id
    assert [r.scenario for r in result.alarms] == ["R8"]
    assert result.unappended == ()


@pytest.mark.unit
def test_a_missing_record_alarms_and_every_passing_record_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tmp_path, "r9", "success", "2026-11-16T14:48:30+00:00", monkeypatch)
    appender = _Appender()
    result = _handler(appender).handle(
        ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    )
    assert [r.scenario for r in result.alarms] == ["R8"]
    assert "conclusion=no-record" in appender.calls[0][0]

    _record(tmp_path, "r8", "success", "2026-11-16T14:48:29+00:00", monkeypatch)
    quiet = _Appender()
    result = _handler(quiet).handle(
        ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    )
    assert quiet.calls == []
    assert result.alarms == ()


@pytest.mark.unit
def test_one_failed_run_has_one_request_id_and_the_next_run_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tmp_path, "r8", "failure", "2026-11-16T14:48:29+00:00", monkeypatch)
    _record(tmp_path, "r9", "success", "2026-11-16T14:48:30+00:00", monkeypatch)
    appender = _Appender()
    handler = _handler(appender)
    handler.handle(ModelScenarioAlarmRequest(home=tmp_path, host="h053"))
    handler.handle(ModelScenarioAlarmRequest(home=tmp_path, host="h053"))
    _record(tmp_path, "r8", "failure", "2026-11-16T20:48:29+00:00", monkeypatch)
    handler.handle(ModelScenarioAlarmRequest(home=tmp_path, host="h053"))
    ids = [request_id for _, request_id in appender.calls]
    assert ids[0] == ids[1]
    assert ids[2] != ids[0]


@pytest.mark.unit
def test_an_unappended_alarm_fails_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tmp_path, "r8", "success", "2026-11-16T14:48:29+00:00", monkeypatch)
    _record(tmp_path, "r9", "failure", "2026-11-16T14:48:30+00:00", monkeypatch)
    stub = tmp_path / "append.sh"
    stub.write_text("#!/bin/sh\ncat >/dev/null\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    alarm_module = importlib.import_module(
        "omnimarket.nodes.node_delegation_scenario_alarm_effect.handlers.handler_delegation_scenario_alarm"
    )

    class _AlarmClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            del tz
            return cls.combine(_NOW.date(), _NOW.time(), _NOW.tzinfo)

    monkeypatch.setattr(alarm_module, "datetime", _AlarmClock)
    state = tmp_path / "state"
    code = main(
        ["--home", str(tmp_path), "--state-dir", str(state)],
        environ={"ONEX_LEDGER_BUS_APPEND_COMMAND": str(stub), "ONEX_HOST_NAME": "h053"},
    )
    assert code == 1
    record = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert record["unappended"] == ["R9"]


@pytest.mark.unit
def test_a_success_record_older_than_the_declared_age_alarms_as_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A unit killed before it wrote its record, or a stopped timer, leaves the last run's success in place.
    settings = load_alarm_settings()
    r8_age = next(w.max_age_seconds for w in settings.scenarios if w.scenario == "R8")
    old = (_NOW - timedelta(seconds=r8_age + 600)).isoformat()
    _record(tmp_path, "r8", "success", old, monkeypatch)
    _record(tmp_path, "r9", "success", "2026-11-16T14:48:30+00:00", monkeypatch)
    appender = _Appender()
    result = _handler(appender).handle(
        ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    )
    assert [r.scenario for r in result.alarms] == ["R8"]
    assert "conclusion=stale" in appender.calls[0][0]


@pytest.mark.unit
def test_a_success_record_inside_the_declared_age_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_alarm_settings()
    for watch in settings.scenarios:
        fresh = (_NOW - timedelta(seconds=watch.max_age_seconds - 60)).isoformat()
        _record(tmp_path, watch.scenario.lower(), "success", fresh, monkeypatch)
    appender = _Appender()
    result = _handler(appender).handle(
        ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    )
    assert appender.calls == []
    assert result.alarms == ()


@pytest.mark.unit
def test_a_success_record_with_an_unreadable_time_alarms_as_stale(
    tmp_path: Path,
) -> None:
    for scenario in ("r8", "r9"):
        path = (
            tmp_path / f".local/state/omni/delegation-scenario-{scenario}/last-run.json"
        )
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"conclusion": "success", "ran_at": "not-a-time"}),
            encoding="utf-8",
        )
    appender = _Appender()
    result = _handler(appender).handle(
        ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    )
    assert len(result.alarms) == 2
    assert all("conclusion=stale" in row for row, _ in appender.calls)


@pytest.mark.unit
def test_the_writers_refusal_record_alarms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tmp_path, "r8", "refused", "2026-11-16T14:48:29+00:00", monkeypatch)
    _record(tmp_path, "r9", "success", "2026-11-16T14:48:30+00:00", monkeypatch)
    appender = _Appender()
    _handler(appender).handle(ModelScenarioAlarmRequest(home=tmp_path, host="h053"))
    assert "conclusion=refused" in appender.calls[0][0]


@pytest.mark.unit
def test_the_fixture_record_has_the_keys_the_live_h053_record_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Read on h053 at 2026-11-16: last-run.json of r8 and r9 are {"conclusion", "ran_at", "scenario"}.
    _record(tmp_path, "r8", "failure", "2026-11-16T14:48:29+00:00", monkeypatch)
    record = json.loads(
        (tmp_path / ".local/state/omni/delegation-scenario-r8/last-run.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(record) == {"conclusion", "ran_at", "scenario"}
    assert record["scenario"] == "R8"


def _missing_record_request_id(clock: datetime, tmp_path: Path) -> str:
    appender = _Appender()
    HandlerDelegationScenarioAlarm(
        load_alarm_settings(), appender, clock=lambda: clock
    ).handle(ModelScenarioAlarmRequest(home=tmp_path, host="h053"))
    return next(
        request_id for row, request_id in appender.calls if "scenario=R8" in row
    )


@pytest.mark.unit
def test_a_later_missing_record_outage_has_its_own_request_id(tmp_path: Path) -> None:
    # The old "unknown" ran_at gave every missing-record outage on a host the same request id, forever.
    bucket = load_alarm_settings().alarm_bucket_seconds
    first = _missing_record_request_id(_NOW, tmp_path)
    same_bucket = _missing_record_request_id(_NOW + timedelta(seconds=60), tmp_path)
    later = _missing_record_request_id(_NOW + timedelta(seconds=bucket + 60), tmp_path)
    assert same_bucket == first
    assert later != first


@pytest.mark.unit
def test_a_second_tick_over_the_same_failure_appends_the_identical_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The work ledger answers a repeated request id duplicate only when the rows are byte-identical,
    # so a row stamped with the tick's own clock would be refused on the second tick of a failed run.
    _record(tmp_path, "r8", "failure", "2026-11-16T14:48:29+00:00", monkeypatch)
    _record(tmp_path, "r9", "success", "2026-11-16T14:48:30+00:00", monkeypatch)
    rows = []
    moments = iter((_NOW, _NOW + timedelta(seconds=900)))
    for _ in range(2):
        appender = _Appender()
        HandlerDelegationScenarioAlarm(
            load_alarm_settings(),
            appender,
            clock=moments.__next__,
        ).handle(ModelScenarioAlarmRequest(home=tmp_path, host="h053"))
        rows.append(appender.calls[0])
    assert rows[0] == rows[1]


@pytest.mark.unit
def test_the_tick_without_an_append_route_refuses(tmp_path: Path) -> None:
    assert main(["--home", str(tmp_path)], environ={}) == 2


@pytest.mark.unit
def test_omn24874_alarm_last_run_record_is_written_when_the_tick_exits_two(
    tmp_path: Path,
) -> None:
    # Review of head 43bc9673d0c1, finding 7: exit 2 (no append route) left no last-run record, so R9 read the previous
    # tick's record, which said success.
    state = tmp_path / "state"
    state.mkdir()
    (state / "last-run.json").write_text(
        json.dumps({"conclusion": "success", "ran_at": "2026-11-16T14:00:00+00:00"}),
        encoding="utf-8",
    )
    code = main(["--home", str(tmp_path), "--state-dir", str(state)], environ={})
    assert code == 2
    record = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert record["conclusion"] == "failure"
    assert "ONEX_LEDGER_BUS_APPEND_COMMAND" in record["error"]
    assert record["ran_at"] != "2026-11-16T14:00:00+00:00"


@pytest.mark.unit
def test_omn24874_alarm_last_run_record_is_written_when_the_appender_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An exception out of the handler (the appender raised) also left no record.
    tick_module = importlib.import_module(
        "omnimarket.nodes.node_delegation_scenario_alarm_effect.runtime_delegation_scenario_alarm_tick"
    )

    class _Raising:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
            raise RuntimeError("the appender broke")

    monkeypatch.setattr(tick_module, "CommandLedgerAppender", _Raising)
    state = tmp_path / "state"
    code = main(
        ["--home", str(tmp_path), "--state-dir", str(state)],
        environ={"ONEX_LEDGER_BUS_APPEND_COMMAND": "true", "ONEX_HOST_NAME": "h053"},
    )
    assert code == 1
    record = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert record["conclusion"] == "failure"
    assert "the appender broke" in record["error"]


@pytest.mark.unit
def test_omn24874_alarm_last_run_record_is_written_when_the_append_command_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review finding 7's check: CommandLedgerAppender catches a timeout, so the record was already written for it.
    _record(tmp_path, "r8", "failure", "2026-11-16T14:48:29+00:00", monkeypatch)

    def _timeout(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(subprocess, "run", _timeout)
    state = tmp_path / "state"
    code = main(
        ["--home", str(tmp_path), "--state-dir", str(state)],
        environ={"ONEX_LEDGER_BUS_APPEND_COMMAND": "append", "ONEX_HOST_NAME": "h053"},
    )
    assert code == 1
    record = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert record["conclusion"] == "failure"
    assert "R8" in record["unappended"]


@pytest.mark.unit
def test_the_installer_declares_the_alarm_timer() -> None:
    contract = yaml.safe_load(
        (
            _REPO
            / "src/omnimarket/nodes/node_host_installer_registry_units/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    units = contract.get("units") or contract
    found = [
        value
        for value in (units.values() if isinstance(units, dict) else [])
        if isinstance(value, dict)
        and value.get("module")
        == "omnimarket.nodes.node_delegation_scenario_alarm_effect.runtime_delegation_scenario_alarm_tick"
    ]
    assert len(found) == 1
    assert found[0]["schedule"]["start_interval"] == 900
    assert (_REPO / found[0]["entry_file"]).is_file()
    assert CONTRACT_PATH.is_file()


def _write_cli_stub(path: Path, argv_log: Path) -> None:
    """A stand-in that records its argv and prints a reply the append command never prints (the negative control)."""
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(argv_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'result': 'ok'}))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.mark.unit
def test_the_rendered_unit_argv_fails_the_tick_on_a_reply_the_cli_never_prints(
    tmp_path: Path,
) -> None:
    # The unit's rendered argv is `--omni-home <dir> --state-dir <dir>`: the tick adds nothing to it. The test appends
    # `--home` itself, only to point the tick at an empty directory of records. The accepted reply is not built here
    # (see the CliRunner tests below); this stub prints a string the CLI never prints, so the tick must exit 1.
    unit = load_units()["scenario-alarm-systemd"]
    argv = HandlerRegistryUnits().tick_arguments(
        request(tmp_path, unit="scenario-alarm-systemd"), unit
    )
    assert argv[0] == "--omni-home"
    assert argv[2] == "--state-dir"
    stub = tmp_path / "append.py"
    argv_log = tmp_path / "append-argv.log"
    _write_cli_stub(stub, argv_log)
    home = tmp_path / "home"
    empty_home = tmp_path / "empty"
    empty_home.mkdir()
    exit_code = main(
        [*argv, "--home", str(empty_home)],
        environ={"ONEX_LEDGER_BUS_APPEND_COMMAND": str(stub), "ONEX_HOST_NAME": "h053"},
    )
    assert exit_code == 1
    # Both alarms (R8 and R9 have no record under the empty home) reached the append command with the CLI's flags.
    calls = [
        json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(calls) == 2
    for call in calls:
        assert call[:2] == ["--lane", "delegation-scenario-alarm"]
        assert call[2:4] == ["--host", "h053"]
        assert call[4] == "--request-id"
        assert call[6:] == ["--rows-file", "-"]
    record = json.loads((home / unit["evidence_file"]).read_text(encoding="utf-8"))
    assert record["alarms"] == ["R8", "R9"]
    assert record["unappended"] == ["R8", "R9"]
    assert record["conclusion"] == "failure"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "unit_key"),
    [("R8", "scenario-r8-systemd"), ("R9", "scenario-r9-systemd")],
)
def test_each_max_age_is_the_start_interval_of_the_scenarios_unit_plus_a_quarter(
    scenario: str, unit_key: str
) -> None:
    # The contract says "R8 21600 + 5400, R9 3600 + 900"; nothing tied those numbers to the units' start_interval until now.
    unit = load_units()[unit_key]
    watch = {w.scenario: w for w in load_alarm_settings().scenarios}[scenario]
    interval = unit["schedule"]["start_interval"]
    assert watch.max_age_seconds == interval + interval // 4
    assert watch.unit == unit["label"]
    assert watch.evidence_file == unit["evidence_file"]
    # Literal pins of the two intervals, so a change to either side is a change to this line too.
    assert interval == {"R8": 21600, "R9": 3600}[scenario]
    assert watch.max_age_seconds == {"R8": 27000, "R9": 4500}[scenario]


class _FileRunner:
    """The file judge's append: writes the rows it is handed to the ledger file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, rows: str) -> ModelAppendCommandResult:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(rows + "\n")
        return ModelAppendCommandResult(exit_code=0)


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["file", "bus"])
def test_the_ledger_takes_the_backdated_stamp_of_an_alarm_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # Missing records recur per bucket. Exercise a tick just before its six-hour
    # boundary against a ledger already holding a newer row.
    del monkeypatch
    now = datetime(2026, 11, 16, 17, 59, 59, tzinfo=UTC)
    captured = _Appender()
    HandlerDelegationScenarioAlarm(
        load_alarm_settings(), captured, clock=lambda: now
    ).handle(ModelScenarioAlarmRequest(home=tmp_path, host="h053"))
    row, request_id = captured.calls[0]
    stamp = datetime.strptime(row.split(" | ")[0], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    assert now - stamp == timedelta(hours=6) - timedelta(seconds=1)

    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        f"{now:%Y-%m-%dT%H:%M:%SZ} | STATUS | lane=newer | principal=alarm-principal | ticket=OMN-24874 | "
        "actor=claude:sonnet:remote-lane | model=sonnet | a row newer than the backdated alarm\n",
        encoding="utf-8",
    )
    store = InMemoryBusLedgerStore()
    handler = HandlerWorkLedgerAppendEffect(
        _FileRunner(ledger),
        LocalLedgerFile(ledger),
        "ledger",
        writer_principals=("alarm-principal",),
        operator_principal="operator",
        fence=LedgerFlipFence(),
        bus_store=store,
    )
    if mode == "bus":
        assert handler.flip_to_bus(drain_timeout_s=5).flipped is True
    receipt = handler.handle(
        ModelWorkLedgerAppendRequest(
            request_id=UUID(request_id),
            rows=stamp_request_cell(row, request_id),
            requested_by_lane="delegation-scenario-alarm",
            requesting_host="h053",
            requested_at=now,
        ),
        writer_principal="alarm-principal",
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED, receipt.message
    landed = ledger.read_text(encoding="utf-8") + store.text()
    assert "alarm=delegation-scenario-r8-failed" in landed


@pytest.mark.unit
def test_runtime_discovery_reads_the_top_level_handler_of_the_alarm_contract() -> None:
    # The runtime's own discovery turns a top-level ``handler:`` into a routing entry (the legacy form), so the
    # runtime does see this class, and the class takes its settings and appender as constructor parameters. The
    # test reads that through discovery's public entry point instead of asserting keys the YAML does not hold.
    manifest = discover_contracts_from_paths([CONTRACT_PATH])
    assert manifest.errors == ()
    (discovered,) = manifest.contracts
    routing = discovered.handler_routing
    assert routing is not None
    (entry,) = routing.handlers
    assert (entry.handler.module, entry.handler.name) == (
        HandlerDelegationScenarioAlarm.__module__,
        HandlerDelegationScenarioAlarm.__name__,
    )
    assert entry.event_model is not None
    assert entry.event_model.name == ModelScenarioAlarmRequest.__name__
    parameters = inspect.signature(HandlerDelegationScenarioAlarm).parameters
    assert [
        name for name, p in parameters.items() if p.default is inspect.Parameter.empty
    ] == ["settings", "appender"]
    # The repository's own discovery imports the class and never calls it.
    module = importlib.import_module(
        "omnimarket.nodes.node_delegation_scenario_alarm_effect"
    )
    probe = _probe_entry_point(
        "node_delegation_scenario_alarm_effect", module.__name__, _REPO / "src"
    )
    assert probe.ok, probe.reason


async def _wire(
    contract_path: Path, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> ModelAutoWiringReport:
    """Run infra's real ``wire_from_manifest`` over one contract with an in-memory bus, as the effects runtime does."""
    if strict:
        monkeypatch.setenv("ONEX_WIRING_STRICT_MODE", "1")
    else:
        monkeypatch.delenv("ONEX_WIRING_STRICT_MODE", raising=False)
    manifest = ModelAutoWiringManifest(
        contracts=tuple(discover_contracts_from_paths([contract_path]).contracts)
    )
    bus = EventBusInmemory()
    container = ModelONEXContainer()
    await container.service_registry.register_instance(ProtocolEventBusPublisher, bus)
    await container.service_registry.register_instance(ProtocolEventBusSubscriber, bus)
    try:
        return await wire_from_manifest(
            manifest,
            MessageDispatchEngine(),
            event_bus=bus,
            environment="local",
            container=container,
        )
    finally:
        await bus.close()


@pytest.mark.unit
@pytest.mark.parametrize("strict", [False, True], ids=["non_strict", "strict"])
async def test_the_effects_runtime_wires_nothing_from_the_alarm_contract_and_never_builds_the_handler(
    monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    # Review finding 6: the contract's top-level handler is discovered as a routing entry, and the class takes two
    # required constructor parameters (settings, appender) that no runtime can supply. Infra builds a handler only for
    # a contract that subscribes to a topic; this one declares no event_bus.subscribe_topics, so the wiring pass reads
    # it as SKIPPED in both modes, and nothing is constructed (no TypeError at boot, strict or not).
    report = await _wire(CONTRACT_PATH, monkeypatch, strict)
    assert [(r.contract_name, r.outcome) for r in report.results] == [
        ("node_delegation_scenario_alarm_effect", EnumWiringOutcome.SKIPPED)
    ]
    assert "subscribe_topics" in str(report.results[0].reason)
    assert report.quarantined_handlers == ()


@pytest.mark.unit
async def test_the_alarm_handler_would_be_unresolvable_if_the_contract_subscribed_to_a_topic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The negative control of the test above: with a subscribed topic the same class is built by the wiring pass, and a
    # constructor it cannot satisfy is a TypeError (re-raised in strict mode), so the skip above is what protects boot.
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    contract["event_bus"] = {
        "subscribe_topics": ["onex.cmd.omnimarket.scenario-alarm-control.v1"]
    }
    control = tmp_path / "contract.yaml"
    control.write_text(yaml.safe_dump(contract), encoding="utf-8")
    with pytest.raises(TypeError):
        await _wire(control, monkeypatch, strict=True)


@pytest.mark.unit
@pytest.mark.parametrize("conclusion", ["failure", "refused"])
def test_a_failed_run_crossing_a_bucket_boundary_has_one_row_and_request_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conclusion: str
) -> None:
    _record(tmp_path, "r8", conclusion, "2026-11-16T17:59:00+00:00", monkeypatch)
    _record(tmp_path, "r9", "success", "2026-11-16T17:59:00+00:00", monkeypatch)
    appender = _Appender()
    moments = iter(
        (
            datetime(2026, 11, 16, 17, 59, 59, tzinfo=UTC),
            datetime(2026, 11, 16, 18, 0, 1, tzinfo=UTC),
        )
    )
    handler = HandlerDelegationScenarioAlarm(
        load_alarm_settings(), appender, clock=moments.__next__
    )
    for _ in range(2):
        handler.handle(ModelScenarioAlarmRequest(home=tmp_path, host="h053"))
    assert len(appender.calls) == 2
    assert appender.calls[0] == appender.calls[1]
    assert appender.calls[0][0].startswith("2026-11-16T17:59:00Z | STATUS")


@pytest.mark.unit
@pytest.mark.parametrize("microsecond", [0, 496205, 999999])
def test_failure_stamp_is_visible_to_a_reader_window_starting_at_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, microsecond: int
) -> None:
    ran_at = datetime(2026, 11, 16, 14, 48, 29, microsecond=microsecond, tzinfo=UTC)
    _record(tmp_path, "r8", "failure", ran_at.isoformat(), monkeypatch)
    _record(tmp_path, "r9", "success", ran_at.isoformat(), monkeypatch)
    ledger = InMemoryLedger()
    handler = HandlerDelegationScenarioAlarm(
        load_alarm_settings(), ledger, clock=lambda: _NOW
    )
    request_ = ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    assert handler.handle(request_).alarms[0].append_status == "accepted"
    assert handler.handle(request_).alarms[0].append_status == "duplicate"
    assert len(ledger.lines) == 1
    reading = ledger.read(
        ModelLedgerQuery(
            since=ran_at,
            until=_NOW + timedelta(seconds=1),
            row_type="STATUS",
            term="alarm=delegation-scenario-r8-failed",
        )
    )
    assert reading.error is None
    assert len(reading.rows) == 1
    assert ran_at <= reading.rows[0].stamp < ran_at + timedelta(seconds=1)


def _r8_rows(appender: _Appender) -> list[tuple[str, str]]:
    return [call for call in appender.calls if "scenario=R8" in call[0]]


def _r8_tick(home: Path, clock: datetime) -> tuple[str, str]:
    appender = _Appender()
    HandlerDelegationScenarioAlarm(
        load_alarm_settings(), appender, clock=lambda: clock
    ).handle(ModelScenarioAlarmRequest(home=home, host="h053"))
    (call,) = _r8_rows(appender)
    return call


@pytest.mark.unit
@pytest.mark.parametrize("conclusion", ["success", "failure", "refused"])
def test_a_record_older_than_the_declared_age_raises_the_bucketed_stale_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conclusion: str
) -> None:
    # A failed or refused run whose unit then stops is as stale as a success: it recurs by bucket, never once forever.
    settings = load_alarm_settings()
    r8_age = next(w.max_age_seconds for w in settings.scenarios if w.scenario == "R8")
    old = (_NOW - timedelta(seconds=r8_age + 600)).isoformat()
    _record(tmp_path, "r8", conclusion, old, monkeypatch)
    one_bucket = (_NOW, _NOW + timedelta(seconds=900))
    next_bucket = _NOW + timedelta(seconds=settings.alarm_bucket_seconds // 2 + 1)
    first = _r8_tick(tmp_path, one_bucket[0])
    again = _r8_tick(tmp_path, one_bucket[1])
    later = _r8_tick(tmp_path, next_bucket)
    cells = first[0].split(" | ")
    assert "conclusion=stale" in cells
    # Stamped at the later of the bucket start (12:00) and ran_at + max_age (14:50): never before the run it cites.
    assert cells[0] == "2026-11-16T14:50:00Z"
    # Ticks in one bucket: the identical row and request id. Ticks in adjacent buckets: neither.
    assert again == first
    assert later[1] != first[1]
    assert later[0] != first[0]
    assert later[0].split(" | ")[0] == "2026-11-16T18:00:00Z"


@pytest.mark.unit
def test_a_no_record_row_and_a_later_record_without_a_time_in_one_bucket_differ(
    tmp_path: Path,
) -> None:
    # Same host, alarm, "unknown" ran_at and bucket, but different bytes: the conclusion is part of a bucketed key.
    first = _r8_tick(tmp_path, _NOW)
    record = tmp_path / ".local/state/omni/delegation-scenario-r8/last-run.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"conclusion": "failure"}), encoding="utf-8")
    second = _r8_tick(tmp_path, _NOW + timedelta(seconds=900))
    assert "conclusion=no-record" in first[0]
    assert "conclusion=failure" in second[0]
    assert first[0] != second[0]
    assert first[1] != second[1]


class _WorkLedgerAppender:
    """The real work-ledger append handler behind the alarm's appender protocol."""

    def __init__(self, ledger: Path, now: datetime) -> None:
        self.ledger = ledger
        self._now = now
        self._handler = HandlerWorkLedgerAppendEffect(
            _FileRunner(ledger),
            LocalLedgerFile(ledger),
            "ledger",
            writer_principals=("alarm-principal",),
            operator_principal="operator",
            fence=LedgerFlipFence(),
            bus_store=InMemoryBusLedgerStore(),
        )

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        receipt = self._handler.handle(
            ModelWorkLedgerAppendRequest(
                request_id=UUID(request_id),
                rows=stamp_request_cell(rows, request_id),
                requested_by_lane="delegation-scenario-alarm",
                requesting_host="h053",
                requested_at=self._now,
            ),
            writer_principal="alarm-principal",
        )
        return ModelAppendOutcome(status=receipt.status.value, message=receipt.message)


@pytest.mark.unit
@pytest.mark.parametrize("conclusion", ["failure", "refused"])
@pytest.mark.parametrize("age", ["recent", "old"])
def test_a_second_tick_over_a_failed_run_is_a_duplicate_and_lands_one_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conclusion: str, age: str
) -> None:
    # One row is shown through the real append handler, not a fake that always accepts.
    settings = load_alarm_settings()
    r8_age = next(w.max_age_seconds for w in settings.scenarios if w.scenario == "R8")
    ran_at = (
        _NOW - timedelta(seconds=600)
        if age == "recent"
        else _NOW - timedelta(seconds=r8_age + 600)
    )
    _record(tmp_path, "r8", conclusion, ran_at.isoformat(), monkeypatch)
    _record(tmp_path, "r9", "success", _NOW.isoformat(), monkeypatch)
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        f"{_NOW:%Y-%m-%dT%H:%M:%SZ} | STATUS | lane=newer | principal=alarm-principal | ticket=OMN-24874 | "
        "actor=claude:sonnet:remote-lane | model=sonnet | a row newer than the alarm\n",
        encoding="utf-8",
    )
    appender = _WorkLedgerAppender(ledger, _NOW)
    clocks = iter((_NOW, _NOW + timedelta(seconds=900)))
    handler = HandlerDelegationScenarioAlarm(settings, appender, clock=clocks.__next__)
    request_ = ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    first = handler.handle(request_)
    second = handler.handle(request_)
    assert [r.append_status for r in first.alarms] == ["accepted"]
    assert [r.append_status for r in second.alarms] == ["duplicate"]
    landed = [
        line
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if "alarm=delegation-scenario-r8-failed" in line
    ]
    assert len(landed) == 1
    assert f"conclusion={'stale' if age == 'old' else conclusion}" in landed[0]


def _accepted_receipt_json(request_id: str) -> str:
    """The line ``omnimarket.work_ledger_bus.cli append`` prints on acceptance: the full receipt, not a bare status."""
    return ModelWorkLedgerAppendReceipt(
        request_id=UUID(request_id),
        status=EnumWorkLedgerAppendStatus.ACCEPTED,
        exit_code=0,
        message="appended",
        ledger_lines=[1],
        ledger_host="h053",
        duration_ms=1,
    ).model_dump_json()


@pytest.mark.unit
def test_the_rendered_append_command_and_flags_reach_the_real_bus_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The unit's declared environment is rendered and its command is split, as the tick splits it; the argv the
    # appender then builds is parsed by the real ``append`` command and its accepted reply is the real receipt.
    unit = load_units()["scenario-alarm-systemd"]
    handler = HandlerRegistryUnits()
    rendered = handler.environment(
        request(tmp_path, unit="scenario-alarm-systemd"), unit
    )
    command = rendered["ONEX_LEDGER_BUS_APPEND_COMMAND"]
    assert "{" not in command
    assert rendered["ONEX_WORKSPACE_CONFIG_ROOT"] == str(tmp_path / "ws")
    base = shlex.split(command)
    assert base[0] == str(tmp_path / "omni" / "omnimarket/.venv/bin/python")
    assert base[1:4] == ["-m", "omnimarket.work_ledger_bus.cli", "append"]
    seen: list[list[str]] = []

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        request_id = argv[argv.index("--request-id") + 1]
        return subprocess.CompletedProcess(
            argv, 0, _accepted_receipt_json(request_id) + "\n", ""
        )

    monkeypatch.setattr(subprocess, "run", _run)
    outcome = CommandLedgerAppender(
        base, "delegation-scenario-alarm", "h053", 120
    ).append("2026-11-16T14:48:29Z | STATUS | lane=x", str(UUID(int=7)))
    assert outcome.status == "accepted"
    (argv,) = seen
    context = append_command.make_context("append", argv[argv.index("append") + 1 :])
    assert context.params["bus_lane"] == "dev"
    assert context.params["omnibase_path"] == tmp_path / "omni"
    assert context.params["lane"] == "delegation-scenario-alarm"
    assert context.params["host"] == "h053"
    assert str(context.params["request_id"]) == str(UUID(int=7))
    assert context.params["rows_file"] == "-"


class _RealAppendCommand:
    """The bus CLI's own ``append`` command, run under CliRunner over the real ledger append handler.

    Only the broker is replaced: the command parses its flags, reads the rows from stdin, sends them through
    ``WorkLedgerAppendCaller``'s seam to the real ``HandlerWorkLedgerAppendEffect`` and prints the receipt it gets.
    Each run's exit code and stdout are kept in ``replies`` and returned to the tick's ``subprocess.run`` call.
    """

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime
    ) -> None:
        self.ledger = tmp_path / "ledger.md"
        self.ledger.write_text(
            f"{now:%Y-%m-%dT%H:%M:%SZ} | STATUS | lane=newer | principal=alarm-principal | ticket=OMN-24874 | "
            "actor=claude:sonnet:remote-lane | model=sonnet | a row newer than the alarm\n",
            encoding="utf-8",
        )
        self.replies: list[tuple[int, str]] = []
        handler = HandlerWorkLedgerAppendEffect(
            _FileRunner(self.ledger),
            LocalLedgerFile(self.ledger),
            "ledger",
            writer_principals=("alarm-principal",),
            operator_principal="operator",
            fence=LedgerFlipFence(),
            bus_store=InMemoryBusLedgerStore(),
        )

        class _Caller:
            def __init__(self, bus: ProtocolLabRunBus, *, principal: str) -> None:
                assert principal == "alarm-principal"

            async def append(
                self, request_: ModelWorkLedgerAppendRequest, *, timeout_s: float
            ) -> ModelWorkLedgerAppendReceipt:
                del timeout_s
                return handler.handle(request_, writer_principal="alarm-principal")

            async def stop(self) -> None:
                return None

        @contextlib.asynccontextmanager
        async def _open_bus(**_: object) -> AsyncIterator[ProtocolLabRunBus]:
            yield EventBusInmemory(environment="local", group="alarm-test")

        def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            result = CliRunner().invoke(
                bus_cli.work_ledger_group,
                argv[argv.index("append") :],
                input=str(kwargs["input"]),
            )
            self.replies.append((result.exit_code, result.stdout))
            return subprocess.CompletedProcess(
                argv, result.exit_code, result.stdout, result.stderr
            )

        monkeypatch.setattr(bus_cli, "open_lab_run_bus", _open_bus)
        monkeypatch.setattr(bus_cli, "bound_lane_principal", lambda: "alarm-principal")
        monkeypatch.setattr(bus_cli, "WorkLedgerAppendCaller", _Caller)
        monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "onex-state"))
        monkeypatch.setattr(subprocess, "run", _run)

    def alarm_lines(self) -> list[str]:
        return [
            line
            for line in self.ledger.read_text(encoding="utf-8").splitlines()
            if "alarm=delegation-scenario-r8-failed" in line
        ]


def _rendered_tick(tmp_path: Path, home: Path) -> tuple[list[str], dict[str, str]]:
    """The unit's rendered tick arguments (plus the records ``--home``) and its rendered environment."""
    unit = load_units()["scenario-alarm-systemd"]
    handler = HandlerRegistryUnits()
    unit_request = request(tmp_path, unit="scenario-alarm-systemd")
    argv = [*handler.tick_arguments(unit_request, unit), "--home", str(home)]
    environ = dict(handler.environment(unit_request, unit))
    environ["ONEX_HOST_NAME"] = "h053"
    return argv, environ


@pytest.mark.unit
def test_omn24874_golden_chain_tick_exit_zero_on_the_accepted_reply_of_the_append_command_under_clirunner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review finding 4: the accepted reply is what the real `append` command prints, not a receipt built in the test.
    now = datetime.now(UTC)
    command = _RealAppendCommand(tmp_path, monkeypatch, now)
    empty_home = tmp_path / "empty"
    empty_home.mkdir()
    argv, environ = _rendered_tick(tmp_path, empty_home)
    assert main(argv, environ=environ) == 0
    # R8 and R9 have no record under the empty home: two alarm rows, two replies, each the CLI's one JSON line.
    assert len(command.replies) == 2
    for exit_code, stdout in command.replies:
        assert exit_code == 0
        (line,) = stdout.splitlines()
        receipt = ModelWorkLedgerAppendReceipt.model_validate_json(line)
        assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
        assert receipt.exit_code == 0
    landed = command.ledger.read_text(encoding="utf-8")
    assert "alarm=delegation-scenario-r8-failed" in landed
    assert "alarm=delegation-scenario-r9-failed" in landed
    state_dir = Path(argv[argv.index("--state-dir") + 1])
    record = json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))
    assert (record["alarms"], record["unappended"]) == (["R8", "R9"], [])
    assert record["conclusion"] == "success"


@pytest.mark.unit
@pytest.mark.parametrize("conclusion", ["failure", "refused"])
def test_omn24874_golden_chain_tick_exit_zero_on_a_duplicate_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conclusion: str
) -> None:
    # Review finding 5: a failed run is resubmitted every 900 s, so the steady state is a duplicate receipt. The
    # command prints status duplicate and exits 0; CommandLedgerAppender maps it to `duplicate`, which the tick
    # counts as appended, so the second tick returns zero and the ledger holds one row.
    now = datetime.now(UTC)
    ran_at = (now - timedelta(seconds=300)).isoformat()
    home = tmp_path / "records"
    _record(home, "r8", conclusion, ran_at, monkeypatch)
    _record(home, "r9", "success", now.isoformat(), monkeypatch)
    command = _RealAppendCommand(tmp_path, monkeypatch, now)
    argv, environ = _rendered_tick(tmp_path, home)
    assert main(argv, environ=environ) == 0
    assert main(argv, environ=environ) == 0
    statuses = [
        json.loads(stdout.splitlines()[-1])["status"] for _, stdout in command.replies
    ]
    assert statuses == ["accepted", "duplicate"]
    assert [exit_code for exit_code, _ in command.replies] == [0, 0]
    assert len(command.alarm_lines()) == 1
    state_dir = Path(argv[argv.index("--state-dir") + 1])
    record = json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))
    assert (record["alarms"], record["unappended"]) == (["R8"], [])
    assert record["conclusion"] == "success"


@pytest.mark.unit
def test_omn24874_golden_chain_tick_exit_one_when_the_append_command_of_the_rendered_unit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The negative control of the exit-zero tests above: on the same rendered argv and environment, an append command
    # that exits 1 leaves the alarm unappended and the tick exits 1 (so a tick that exits 1 fails `-k tick_exit`'s peers).
    now = datetime.now(UTC)
    home = tmp_path / "records"
    _record(
        home, "r8", "failure", (now - timedelta(seconds=300)).isoformat(), monkeypatch
    )
    _record(home, "r9", "success", now.isoformat(), monkeypatch)

    def _fail(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "refused")

    monkeypatch.setattr(subprocess, "run", _fail)
    argv, environ = _rendered_tick(tmp_path, home)
    assert main(argv, environ=environ) == 1
    state_dir = Path(argv[argv.index("--state-dir") + 1])
    record = json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))
    assert (record["alarms"], record["unappended"]) == (["R8"], ["R8"])
    assert record["conclusion"] == "failure"


@pytest.mark.unit
@pytest.mark.parametrize("scenario", ["r8", "r9"])
@pytest.mark.parametrize(
    ("overdue_seconds", "stamp_is_bucket_start"), [(600, False), (30000, True)]
)
def test_omn24874_alarm_stale_row_is_stamped_no_earlier_than_the_run_it_cites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    overdue_seconds: int,
    stamp_is_bucket_start: bool,
) -> None:
    # Review finding 8: R9's max_age (4500 s) is far inside the six-hour bucket, so the bucket start is usually before the
    # ran_at the row cites, and a reader window that starts at ran_at would miss it. The stamp is the later of the
    # bucket start and ran_at + max_age (the moment the record went stale).
    settings = load_alarm_settings()
    max_age = next(
        w.max_age_seconds for w in settings.scenarios if w.scenario == scenario.upper()
    )
    ran_at = _NOW - timedelta(seconds=max_age + overdue_seconds)
    _record(tmp_path, scenario, "success", ran_at.isoformat(), monkeypatch)
    appender = _Appender()
    HandlerDelegationScenarioAlarm(settings, appender, clock=lambda: _NOW).handle(
        ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    )
    (row, _) = next(c for c in appender.calls if f"scenario={scenario.upper()}" in c[0])
    cells = row.split(" | ")
    stamp = datetime.strptime(cells[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    bucket_start = datetime.fromtimestamp(
        (int(_NOW.timestamp()) // settings.alarm_bucket_seconds)
        * settings.alarm_bucket_seconds,
        UTC,
    )
    assert "conclusion=stale" in cells
    assert stamp == max(bucket_start, ran_at + timedelta(seconds=max_age))
    if stamp_is_bucket_start:
        assert stamp == bucket_start
    else:
        assert stamp > bucket_start
    # A reader window that starts at the run's own time finds the row; the stamp is never in the future either.
    assert ran_at <= stamp <= _NOW


def _stamp_of_r8_row(appender: _Appender) -> datetime:
    (row, _) = next(c for c in appender.calls if "scenario=R8" in c[0])
    return datetime.strptime(row.split(" | ")[0], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )


def _write_literal_stale_record(home: Path, ran_at: datetime) -> None:
    """A hand-written fresh record that concludes the literal ``stale`` (the writer's enum has no such value)."""
    watch = next(w for w in load_alarm_settings().scenarios if w.scenario == "R8")
    record = home / watch.evidence_file
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps({"conclusion": "stale", "ran_at": ran_at.isoformat()}),
        encoding="utf-8",
    )


_SECOND_TICK_CASES = {
    # case: (first tick, second tick) over one record; the record is fixed per case (see the test body).
    "future_ran_at": (_NOW, _NOW + timedelta(seconds=900)),
    "fresh_literal_stale": (_NOW, _NOW + timedelta(seconds=900)),
    "fractional_ran_at_read_in_its_own_second": (
        _NOW + timedelta(microseconds=700000),
        _NOW + timedelta(seconds=900, microseconds=700000),
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize("case", list(_SECOND_TICK_CASES))
def test_omn24874_alarm_second_tick_is_a_duplicate_through_the_real_append_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    # Review of head 43bc9673d0c1, finding 1: the stamp was `min(moment, now)`, so a failed record with a future ran_at,
    # a fresh record concluding the literal `stale` and a fractional ran_at read in its own second were stamped at each
    # tick's own second while the request id stayed fixed. The ledger answers a repeated id duplicate only for identical
    # bytes, so the second tick read a conflict and the tick exited 1. The stamp now comes from the record and the bucket.
    first_tick, second_tick = _SECOND_TICK_CASES[case]
    if case == "future_ran_at":
        _record(
            tmp_path,
            "r8",
            "failure",
            (_NOW + timedelta(seconds=3600)).isoformat(),
            monkeypatch,
        )
    elif case == "fresh_literal_stale":
        _write_literal_stale_record(tmp_path, _NOW - timedelta(seconds=600))
    else:
        _record(
            tmp_path,
            "r8",
            "failure",
            (_NOW + timedelta(microseconds=400000)).isoformat(),
            monkeypatch,
        )
    _record(tmp_path, "r9", "success", _NOW.isoformat(), monkeypatch)
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        f"{_NOW + timedelta(days=1):%Y-%m-%dT%H:%M:%SZ} | STATUS | lane=newer | principal=alarm-principal | "
        "ticket=OMN-24874 | actor=claude:sonnet:remote-lane | model=sonnet | a row newer than the alarm\n",
        encoding="utf-8",
    )
    appender = _WorkLedgerAppender(ledger, _NOW + timedelta(days=1))
    clocks = iter((first_tick, second_tick))
    handler = HandlerDelegationScenarioAlarm(
        load_alarm_settings(), appender, clock=clocks.__next__
    )
    request_ = ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    first = handler.handle(request_)
    second = handler.handle(request_)
    assert [r.append_status for r in first.alarms] == ["accepted"]
    assert [r.append_status for r in second.alarms] == ["duplicate"]
    assert second.unappended == ()
    assert (
        len(
            [
                line
                for line in ledger.read_text(encoding="utf-8").splitlines()
                if "alarm=delegation-scenario-r8-failed" in line
            ]
        )
        == 1
    )


@pytest.mark.unit
@pytest.mark.parametrize("case", list(_SECOND_TICK_CASES))
def test_omn24874_alarm_second_tick_stamp_is_a_function_of_the_record_and_bucket_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    # The same three records, read at two tick moments of one bucket: the identical row bytes and request id.
    first_tick, second_tick = _SECOND_TICK_CASES[case]
    if case == "future_ran_at":
        _record(
            tmp_path,
            "r8",
            "failure",
            (_NOW + timedelta(seconds=3600)).isoformat(),
            monkeypatch,
        )
    elif case == "fresh_literal_stale":
        _write_literal_stale_record(tmp_path, _NOW - timedelta(seconds=600))
    else:
        _record(
            tmp_path,
            "r8",
            "failure",
            (_NOW + timedelta(microseconds=400000)).isoformat(),
            monkeypatch,
        )
    first = _r8_tick(tmp_path, first_tick)
    second = _r8_tick(tmp_path, second_tick)
    assert first == second
    if case == "fractional_ran_at_read_in_its_own_second":
        # Rounded up from the record's own time, never clamped to the tick's second.
        assert first[0].startswith("2026-11-16T15:00:01Z | STATUS")
    else:
        # A future ran_at and a fresh literal `stale` are bucketed readings: stamped at the bucket start (12:00).
        assert first[0].startswith("2026-11-16T12:00:00Z | STATUS")


@pytest.mark.unit
def test_omn24874_alarm_future_ran_at_is_not_read_as_the_run_it_cites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A run time ahead of the tick cannot identify a run: it reads as unreadable (bucketed), so its row is never stamped
    # after the tick, and it names its own conclusion in the request id.
    _record(
        tmp_path,
        "r8",
        "failure",
        (_NOW + timedelta(seconds=600)).isoformat(),
        monkeypatch,
    )
    appender = _Appender()
    HandlerDelegationScenarioAlarm(
        load_alarm_settings(), appender, clock=lambda: _NOW
    ).handle(ModelScenarioAlarmRequest(home=tmp_path, host="h053"))
    assert _stamp_of_r8_row(appender) <= _NOW


@pytest.mark.unit
def test_omn24874_alarm_enum_scenario_conclusion_has_no_stale_value() -> None:
    # The values the writer can record: `stale` is the alarm's own reading and is never a recorded conclusion.
    assert {c.value for c in EnumScenarioConclusion} == {
        "success",
        "failure",
        "cancelled",
        "timed_out",
        "skipped",
        "startup_failure",
        "queued",
        "in_progress",
    }


@pytest.mark.unit
def test_the_rendered_unit_environment_leaves_the_state_dir_and_the_lane_login_to_the_real_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review finding 7: the chain replaces `bound_lane_principal`, the bus and the caller, and sets ONEX_STATE_DIR itself.
    # What the unit really gives the append command is read here without those replacements: the rendered environment
    # holds no ONEX_STATE_DIR, so the real command's state directory is the unit's own HOME (`~/.onex/state`), and its
    # lane login comes from the lane declaration under ONEX_WORKSPACE_CONFIG_ROOT (`--bus-lane dev`), not from the
    # environment, so with no bound bus the login read raises instead of resolving.
    from omnimarket.delegated_test_loop.lane_bus import (
        LabRunBusError,
        bound_lane_principal,
    )
    from omnimarket.handlers.bus_binding.select import default_state_dir

    unit = load_units()["scenario-alarm-systemd"]
    rendered = HandlerRegistryUnits().environment(
        request(tmp_path, unit="scenario-alarm-systemd"), unit
    )
    assert "ONEX_STATE_DIR" not in rendered
    assert not [name for name in rendered if "KAFKA" in name or "SASL" in name]
    assert rendered["ONEX_WORKSPACE_CONFIG_ROOT"] == str(tmp_path / "ws")
    assert shlex.split(rendered["ONEX_LEDGER_BUS_APPEND_COMMAND"])[-4:-2] == [
        "--bus-lane",
        "dev",
    ]
    unit_home = tmp_path / "unit-home"
    for name, value in rendered.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(unit_home))
    assert default_state_dir() == unit_home / ".onex" / "state"
    with pytest.raises(LabRunBusError):
        bound_lane_principal()


@pytest.mark.unit
@pytest.mark.parametrize("scenario", ["R8", "R9"])
@pytest.mark.parametrize("skips", [1, 2, 3])
def test_consecutive_dev_skips_raise_only_the_not_ready_alarm(
    tmp_path: Path, scenario: str, skips: int
) -> None:
    settings = load_alarm_settings()
    for watch in settings.scenarios:
        path = tmp_path / watch.evidence_file
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "scenario": watch.scenario,
                    "ran_at": _NOW.isoformat(),
                    "conclusion": "skipped"
                    if watch.scenario == scenario
                    else "success",
                    "consecutive_skips": skips if watch.scenario == scenario else 0,
                    "last_success_at": (_NOW - timedelta(hours=7)).isoformat(),
                }
            ),
            encoding="utf-8",
        )
    appender = _Appender()
    handler = _handler(appender)
    request = ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    result = handler.handle(request)
    reading = next(record for record in result.readings if record.scenario == scenario)
    assert reading.last_success_at == (_NOW - timedelta(hours=7)).isoformat()
    if skips < 3:
        assert result.alarms == ()
        assert appender.calls == []
    else:
        assert len(result.alarms) == 1
        assert result.alarms[0].conclusion == "skipped"
        assert result.alarms[0].alarm_kind is EnumScenarioAlarm.DEV_NOT_READY
        row, request_id = appender.calls[0]
        assert (
            f"alarm=delegation-scenario-{scenario.lower()}-dev-not-ready"
            in row.split(" | ")
        )
        handler.handle(request)
        assert appender.calls[-1] == (row, request_id)
