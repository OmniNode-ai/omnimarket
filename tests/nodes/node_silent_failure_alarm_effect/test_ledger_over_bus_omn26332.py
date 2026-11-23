# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The silent failure alarm reads and appends its ledger rows over the bus and opens no ledger file (OMN-26332).

The alarm used to read a ledger file named by its options and append through a file appender run on that path.
Now its default ledger runs the rows loader node for its window of rows and the work ledger append command for each
row. The failing control is the old route: with a ledger file in place and no loader answer, the tick must read the
window unread and append nothing to the file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.ledger_rows_unreadable_error import LedgerRowsUnreadableError
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.alarm_io import (
    ALARM_LANE,
    LEDGER_COMMAND_ENV,
    READER,
    BusAlarmLedger,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
    HandlerSilentFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelSilentFailureAlarmRequest,
)
from tests.helpers.fake_alarm_ledger import LEDGER_FIXTURE_NAME
from tests.nodes.node_silent_failure_alarm_compute.test_golden_chain_silent_failure_alarm_compute import (
    HOST_NOW,
    RECORDED,
    Watch,
    world,
)

pytestmark = pytest.mark.unit

LOADER = "omnimarket.nodes.node_ledger_rows_loader_effect"
real_run = subprocess.run


class FakeBus:
    """Answers the rows loader and the append command; every other command runs for real."""

    def __init__(self, rows: list[str], *, loader_exit: int = 0) -> None:
        self.rows = rows
        self.loader_exit = loader_exit
        self.loader_calls: list[list[str]] = []
        self.appends: list[tuple[list[str], str]] = []

    def __call__(self, argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        if LOADER in args:
            self.loader_calls.append(args)
            return subprocess.CompletedProcess(
                args, self.loader_exit, "\n".join(self.rows), "refused"
            )
        if "append" in args and "--request-id" in args:
            self.appends.append((args, kwargs["input"]))
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(argv, **kwargs)


def install(monkeypatch: pytest.MonkeyPatch, bus: FakeBus) -> None:
    monkeypatch.setattr(subprocess, "run", bus)


def test_golden_rows_come_from_the_loader_under_the_alarms_reader_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus(["row one", "row two"])
    install(monkeypatch, bus)
    assert BusAlarmLedger().read_rows(24) == "row one\nrow two"
    assert bus.loader_calls == [
        [sys.executable, "-m", LOADER, "--reader", READER, "--window-hours", "24"]
    ]


def test_error_a_loader_that_refuses_is_an_unread_window_never_an_empty_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, FakeBus([], loader_exit=1))
    with pytest.raises(LedgerRowsUnreadableError):
        BusAlarmLedger().read_rows(24)


def test_golden_an_append_sends_the_row_on_standard_input_under_the_alarms_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus([])
    install(monkeypatch, bus)
    row = "2026-11-01T15:33:25Z | STATUS | lane=silent-failure-alarm | ticket=OMN-19998 | text"
    assert BusAlarmLedger().append(row)[0] is True
    assert BusAlarmLedger().append(row)[0] is True
    (first, first_input), (second, _) = bus.appends
    assert first_input == row
    assert first[first.index("--lane") + 1] == ALARM_LANE
    assert (
        first[first.index("--request-id") + 1]
        == second[second.index("--request-id") + 1]
    )
    assert LEDGER_COMMAND_ENV == "ONEX_WORK_LEDGER_COMMAND"


def test_golden_a_tick_reads_and_appends_over_the_bus_and_leaves_the_file_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = world(tmp_path)
    fixture = tmp_path / LEDGER_FIXTURE_NAME
    rows = fixture.read_text(encoding="utf-8").splitlines()
    before = fixture.read_text(encoding="utf-8")
    bus = FakeBus(rows)
    install(monkeypatch, bus)
    watches = {host: Watch(_not_running()) for host in RECORDED}
    result = HandlerSilentFailureAlarm(
        options, host_watches=watches, ledger=BusAlarmLedger()
    ).handle(ModelSilentFailureAlarmRequest(now=HOST_NOW, apply=True))
    assert result.exit_code == 0, result.warnings
    assert result.alarms == ("quiet-host",)
    assert len(bus.loader_calls) == 1
    assert [row for _, row in bus.appends] == list(result.rows)
    assert fixture.read_text(encoding="utf-8") == before


def test_failing_control_a_ledger_file_in_place_is_not_read_when_the_loader_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = world(tmp_path)
    assert (tmp_path / LEDGER_FIXTURE_NAME).is_file()
    bus = FakeBus([], loader_exit=1)
    install(monkeypatch, bus)
    watches = {host: Watch(_not_running()) for host in RECORDED}
    result = HandlerSilentFailureAlarm(
        options, host_watches=watches, ledger=BusAlarmLedger()
    ).handle(ModelSilentFailureAlarmRequest(now=HOST_NOW, apply=True))
    assert result.exit_code == 2
    assert result.alarms == ()
    assert "ledger could not be read" in result.warnings[0]
    assert bus.appends == []


def _not_running() -> Any:
    from omnimarket.models.silent_failure_alarm import ModelDrainerObservation

    return ModelDrainerObservation(job_state="not-running", status_readable=False)
