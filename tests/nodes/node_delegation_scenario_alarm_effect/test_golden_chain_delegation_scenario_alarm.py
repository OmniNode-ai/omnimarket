# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed delegation scenario run writes one ledger alarm row (OMN-24874)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_delegation_scenario_alarm_effect.handlers.handler_delegation_scenario_alarm import (
    CONTRACT_PATH,
    HandlerDelegationScenarioAlarm,
    ModelScenarioAlarmRequest,
    load_alarm_settings,
)
from omnimarket.nodes.node_delegation_scenario_alarm_effect.runtime_delegation_scenario_alarm_tick import (
    main,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome

_NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
_REPO = Path(__file__).resolve().parents[3]


class _Appender:
    def __init__(self, status: str = "accepted") -> None:
        self.calls: list[tuple[str, str]] = []
        self._status = status

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.calls.append((rows, request_id))
        return ModelAppendOutcome(status=self._status)


def _record(home: Path, scenario: str, conclusion: str, ran_at: str) -> None:
    path = home / f".local/state/omni/delegation-scenario-{scenario}/last-run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"conclusion": conclusion, "ran_at": ran_at, "scenario": scenario.upper()}
        ),
        encoding="utf-8",
    )


def _handler(appender: _Appender) -> HandlerDelegationScenarioAlarm:
    return HandlerDelegationScenarioAlarm(
        load_alarm_settings(), appender, clock=lambda: _NOW
    )


@pytest.mark.unit
def test_a_failed_r8_run_writes_one_alarm_row_and_a_passing_r9_writes_none(
    tmp_path: Path,
) -> None:
    _record(tmp_path, "r8", "failure", "2026-11-16T14:48:29+00:00")
    _record(tmp_path, "r9", "success", "2026-11-16T14:48:30+00:00")
    appender = _Appender()
    result = _handler(appender).handle(
        ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    )
    assert len(appender.calls) == 1
    row, request_id = appender.calls[0]
    cells = row.split(" | ")
    assert cells[0] == "2026-11-16T15:00:00Z"
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
    tmp_path: Path,
) -> None:
    _record(tmp_path, "r9", "success", "2026-11-16T14:48:30+00:00")
    appender = _Appender()
    result = _handler(appender).handle(
        ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    )
    assert [r.scenario for r in result.alarms] == ["R8"]
    assert "conclusion=no-record" in appender.calls[0][0]

    _record(tmp_path, "r8", "success", "2026-11-16T14:48:29+00:00")
    quiet = _Appender()
    result = _handler(quiet).handle(
        ModelScenarioAlarmRequest(home=tmp_path, host="h053")
    )
    assert quiet.calls == []
    assert result.alarms == ()


@pytest.mark.unit
def test_one_failed_run_has_one_request_id_and_the_next_run_a_new_one(
    tmp_path: Path,
) -> None:
    _record(tmp_path, "r8", "failure", "2026-11-16T14:48:29+00:00")
    _record(tmp_path, "r9", "success", "2026-11-16T14:48:30+00:00")
    appender = _Appender()
    handler = _handler(appender)
    handler.handle(ModelScenarioAlarmRequest(home=tmp_path, host="h053"))
    handler.handle(ModelScenarioAlarmRequest(home=tmp_path, host="h053"))
    _record(tmp_path, "r8", "failure", "2026-11-16T20:48:29+00:00")
    handler.handle(ModelScenarioAlarmRequest(home=tmp_path, host="h053"))
    ids = [request_id for _, request_id in appender.calls]
    assert ids[0] == ids[1]
    assert ids[2] != ids[0]


@pytest.mark.unit
def test_an_unappended_alarm_fails_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tmp_path, "r8", "success", "2026-11-16T14:48:29+00:00")
    _record(tmp_path, "r9", "failure", "2026-11-16T14:48:30+00:00")
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
