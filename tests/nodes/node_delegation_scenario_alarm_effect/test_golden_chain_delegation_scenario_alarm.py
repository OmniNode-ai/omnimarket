# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed delegation scenario run writes one ledger alarm row (OMN-24874)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.handlers import delegation_readiness_scenario as readiness_scenario
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
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
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


class _Conclusion(Enum):
    SUCCESS = "success"
    FAILURE = "failure"


class _Result(BaseModel):
    conclusion: _Conclusion


class _Scenario:
    def __init__(self, conclusion: str | None) -> None:
        self._conclusion = conclusion

    def run(self) -> _Result:
        if self._conclusion is None:
            raise RuntimeError("no ledger route")
        return _Result(conclusion=_Conclusion(self._conclusion))


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
    # stamped with the start of its 6-hour alarm bucket, not the tick clock
    assert cells[0] == "2026-11-16T12:00:00Z"
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
    for offset in (0, 900):
        appender = _Appender()
        HandlerDelegationScenarioAlarm(
            load_alarm_settings(),
            appender,
            clock=lambda offset=offset: _NOW + timedelta(seconds=offset),
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


@pytest.mark.unit
def test_the_rendered_unit_state_dir_receives_the_ticks_declared_evidence_file(
    tmp_path: Path,
) -> None:
    # The argv the installer renders (--state-dir) must land this tick's record at the unit's declared evidence_file.
    unit = load_units()["scenario-alarm-systemd"]
    argv = HandlerRegistryUnits().tick_arguments(
        request(tmp_path, unit="scenario-alarm-systemd"), unit
    )
    state_dir = Path(argv[argv.index("--state-dir") + 1])
    stub = tmp_path / "append.sh"
    stub.write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    home = tmp_path / "home"
    code = main(
        ["--home", str(tmp_path / "empty"), "--state-dir", str(state_dir)],
        environ={"ONEX_LEDGER_BUS_APPEND_COMMAND": str(stub), "ONEX_HOST_NAME": "h053"},
    )
    assert code in (0, 1)
    assert (home / unit["evidence_file"]).is_file()
