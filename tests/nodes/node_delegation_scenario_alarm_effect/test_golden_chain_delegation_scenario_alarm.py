# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed delegation scenario run writes one ledger alarm row (OMN-24874)."""

from __future__ import annotations

import importlib
import inspect
import json
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import (
    discover_contracts_from_paths,
)

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
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            del tz
            return moment

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
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            del tz
            return _NOW

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


def _write_cli_stub(path: Path, argv_log: Path, *, real_reply: bool) -> None:
    """A stand-in for ``omnimarket.work_ledger_bus.cli append``: records its argv, prints a reply.

    ``real_reply`` prints the receipt the CLI prints on acceptance (``receipt.model_dump_json()``); otherwise a
    reply the CLI never prints.
    """
    reply = (
        "ModelWorkLedgerAppendReceipt(request_id=UUID(rid), status='accepted', exit_code=0, message='appended', "
        "ledger_lines=[1], ledger_host='h053', duration_ms=1).model_dump_json()"
        if real_reply
        else "json.dumps({'result': 'ok'})"
    )
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from uuid import UUID\n"
        "from omnimarket.models.work_ledger_append.model_work_ledger_append import ModelWorkLedgerAppendReceipt\n"
        f"open({str(argv_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.stdin.read()\n"
        "rid = sys.argv[sys.argv.index('--request-id') + 1]\n"
        f"print({reply})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.mark.unit
@pytest.mark.parametrize(("real_reply", "code"), [(True, 0), (False, 1)])
def test_the_rendered_unit_argv_runs_the_tick_to_exit_zero_on_the_real_accepted_reply(
    tmp_path: Path, real_reply: bool, code: int
) -> None:
    # The argv the installer renders (--omni-home, --state-dir) is the tick's whole argument list: nothing is added,
    # and the append command is the one the unit declares. The accepted reply is the CLI's full receipt, so a
    # stub that prints a string the CLI never prints must fail the tick (the negative control).
    unit = load_units()["scenario-alarm-systemd"]
    argv = HandlerRegistryUnits().tick_arguments(
        request(tmp_path, unit="scenario-alarm-systemd"), unit
    )
    assert argv[0] == "--omni-home"
    assert argv[2] == "--state-dir"
    stub = tmp_path / "append.py"
    argv_log = tmp_path / "append-argv.log"
    _write_cli_stub(stub, argv_log, real_reply=real_reply)
    home = tmp_path / "home"
    empty_home = tmp_path / "empty"
    empty_home.mkdir()
    exit_code = main(
        [*argv, "--home", str(empty_home)],
        environ={"ONEX_LEDGER_BUS_APPEND_COMMAND": str(stub), "ONEX_HOST_NAME": "h053"},
    )
    assert exit_code == code
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
    assert record["unappended"] == ([] if real_reply else ["R8", "R9"])
    assert record["conclusion"] == ("success" if real_reply else "failure")


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
    # Stamped at the bucket start (12:00), so a reader's bucket window finds it.
    assert cells[0] == "2026-11-16T12:00:00Z"
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
