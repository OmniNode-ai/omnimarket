# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI JSON receipts and exit codes with a stubbed bus (OMN-20275)."""

import contextlib
import fcntl
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import BusKind, LabRunBusError
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.work_ledger_bus import cli
from omnimarket.work_ledger_bus.cli import work_ledger_group

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("status", "command_code", "expected"),
    [
        ("accepted", 0, 0),
        ("duplicate", 0, 0),
        ("refused", 65, 65),
        ("error", 75, 70),
        ("error", 124, 70),
        ("error", 1, 70),
        ("pending", 0, 75),
        ("bus_unavailable", 0, 69),
    ],
)
def test_append_exit_codes_and_one_json_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
    command_code: int,
    expected: int,
) -> None:
    opened_args: list[dict[str, object]] = []
    requests: list[ModelWorkLedgerAppendRequest] = []
    request_id = uuid4()
    rows = f"2026-10-01T12:00:00Z | STATUS | req={request_id} | exact\ncontinuation\n"
    stopped: list[bool] = []

    @contextlib.asynccontextmanager
    async def open_bus(
        *,
        bus: BusKind,
        lane: str | None,
        kafka_bootstrap: str | None,
        omni_home: Path | None,
    ) -> AsyncIterator[ProtocolLabRunBus]:
        opened_args.append(
            {"bus": bus, "lane": lane, "bootstrap": kafka_bootstrap, "root": omni_home}
        )
        if status == "bus_unavailable":
            raise LabRunBusError("lane login unavailable")
        yield EventBusInmemory(environment="local", group="cli-test")

    class Caller:
        def __init__(self, bus: ProtocolLabRunBus) -> None:
            pass

        async def append(
            self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
        ) -> ModelWorkLedgerAppendReceipt:
            assert timeout_s == 2.5
            requests.append(request)
            if status == "pending":
                raise TimeoutError
            return ModelWorkLedgerAppendReceipt(
                request_id=request.request_id,
                status=EnumWorkLedgerAppendStatus(status),
                exit_code=command_code,
                message="output tail",
                ledger_lines=[12],
                ledger_host="ledger",
                duration_ms=10,
            )

        async def stop(self) -> None:
            stopped.append(True)

    monkeypatch.setattr(cli, "open_lab_run_bus", open_bus)
    monkeypatch.setattr(cli, "WorkLedgerAppendCaller", Caller)
    result = CliRunner().invoke(
        cli.work_ledger_group,
        [
            "append",
            "--bus-lane",
            "declared",
            "--lane",
            "request-lane",
            "--host",
            "lab-host",
            "--request-id",
            str(request_id),
            "--rows-file",
            "-",
            "--timeout-s",
            "2.5",
            "--omnibase-path",
            str(tmp_path),
            "--kafka-bootstrap",
            "broker",
        ],
        input=rows,
    )
    assert result.exit_code == expected, result.output
    assert opened_args == [
        {"bus": "kafka", "lane": "declared", "bootstrap": "broker", "root": tmp_path}
    ]
    if status == "bus_unavailable":
        assert result.stdout == ""
        assert "lane login unavailable" in result.stderr
        assert stopped == []
        return
    assert len(result.stdout.splitlines()) == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == status
    assert payload["request_id"] == str(request_id)
    if status == "pending":
        assert payload == {"status": "pending", "request_id": str(request_id)}
    else:
        assert payload["exit_code"] == command_code
        assert payload["ledger_lines"] == [12]
    assert requests[0].rows == rows
    assert requests[0].requested_by_lane == "request-lane"
    assert requests[0].requesting_host == "lab-host"
    assert requests[0].requested_at.tzinfo is not None
    assert stopped == [True]


def test_append_reads_rows_file_and_defaults_to_60_seconds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows_file = tmp_path / "rows.txt"
    rows_file.write_text("exact rows\n")
    requests: list[ModelWorkLedgerAppendRequest] = []
    timeouts: list[float] = []

    class Caller:
        def __init__(self, bus: ProtocolLabRunBus) -> None:
            pass

        async def append(
            self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
        ) -> ModelWorkLedgerAppendReceipt:
            requests.append(request)
            timeouts.append(timeout_s)
            raise TimeoutError

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(cli, "WorkLedgerAppendCaller", Caller)
    result = CliRunner().invoke(
        cli.work_ledger_group,
        [
            "append",
            "--bus",
            "inmemory",
            "--lane",
            "lab",
            "--host",
            "host",
            "--request-id",
            str(uuid4()),
            "--rows-file",
            str(rows_file),
        ],
    )
    assert result.exit_code == 75
    assert requests[0].rows == "exact rows\n"
    assert timeouts == [60.0]


@pytest.mark.parametrize("argv", ["", "   ", "'unterminated"])
def test_serve_refuses_empty_or_unparseable_append_command(
    tmp_path: Path, argv: str
) -> None:
    result = CliRunner().invoke(
        cli.work_ledger_group,
        [
            "serve",
            "--host-name",
            "ledger",
            "--ledger",
            str(tmp_path / "ledger.md"),
            "--append-command",
            argv,
        ],
    )
    assert result.exit_code == 2
    assert "--append-command" in result.output


def test_serve_refuses_while_another_serve_holds_the_ledger_lock(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("## Work ledger\n", encoding="utf-8")
    lock_path = ledger.with_name(ledger.name + ".work-ledger-serve.lock")
    with lock_path.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = CliRunner().invoke(
            work_ledger_group,
            [
                "serve",
                "--bus",
                "inmemory",
                "--host-name",
                "ledger-host",
                "--ledger",
                str(ledger),
                "--append-command",
                "true",
            ],
        )
    assert result.exit_code == 75
    assert "holds" in result.output
