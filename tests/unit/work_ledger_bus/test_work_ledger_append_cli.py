# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI JSON receipts and exit codes with a stubbed bus (OMN-20275)."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.delegated_test_loop import lane_bus
from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import BusKind, LabRunBusError
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    BusLaneLedgerAppender as LabFillBusLaneLedgerAppender,
)
from omnimarket.nodes.node_pr_handoff_ledger_effect.handlers.handler_pr_handoff_ledger_effect import (
    BusLaneLedgerAppender,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerAppendCommand,
    LocalLedgerFile,
)
from omnimarket.work_ledger_bus import cli
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendHost
from omnimarket.work_ledger_bus.cli import work_ledger_group

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def writer_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_WORK_LEDGER_WRITER_PRINCIPALS", "operator")
    monkeypatch.setenv("ONEX_WORK_LEDGER_OPERATOR_PRINCIPAL", "operator")


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
        def __init__(self, bus: ProtocolLabRunBus, *, principal: str) -> None:
            assert principal == "lane-principal"

        async def start(self) -> None:
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
    monkeypatch.setattr(cli, "bound_lane_principal", lambda: "lane-principal")
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
        def __init__(self, bus: ProtocolLabRunBus, *, principal: str) -> None:
            assert principal == "operator"

        async def start(self) -> None:
            pass

        async def append(
            self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
        ) -> ModelWorkLedgerAppendReceipt:
            requests.append(request)
            timeouts.append(timeout_s)
            raise TimeoutError

        async def stop(self) -> None:
            pass

    @contextlib.asynccontextmanager
    async def open_bus(**kwargs: object) -> AsyncIterator[ProtocolLabRunBus]:
        yield EventBusInmemory(environment="local", group="cli-test")

    monkeypatch.setattr(cli, "open_lab_run_bus", open_bus)
    monkeypatch.setattr(cli, "WorkLedgerAppendCaller", Caller)

    def forbidden_principal() -> str:
        pytest.fail("the in-memory caller must use its explicit principal")

    monkeypatch.setattr(cli, "bound_lane_principal", forbidden_principal)
    result = CliRunner().invoke(
        cli.work_ledger_group,
        [
            "append",
            "--bus",
            "inmemory",
            "--principal",
            "operator",
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


def test_append_without_bound_broker_login_exits_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    @contextlib.asynccontextmanager
    async def open_bus(**kwargs: object) -> AsyncIterator[ProtocolLabRunBus]:
        bus = EventBusInmemory(environment="local", group="cli-test")
        await bus.start()
        try:
            yield bus
        finally:
            await bus.close()
            closed.append(True)

    monkeypatch.setattr(cli, "open_lab_run_bus", open_bus)
    result = CliRunner().invoke(
        cli.work_ledger_group,
        [
            "append",
            "--bus",
            "kafka",
            "--lane",
            "lab",
            "--host",
            "host",
            "--request-id",
            str(uuid4()),
        ],
        input="rows",
    )
    assert result.exit_code == 69, result.output
    assert result.stdout == ""
    assert "this lane resolves no broker login" in result.stderr
    assert closed == [True]


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


@pytest.mark.parametrize(
    "appender_type", [BusLaneLedgerAppender, LabFillBusLaneLedgerAppender]
)
def test_broker_attributed_cli_request_round_trips_through_host_and_local_append(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    appender_type: type[BusLaneLedgerAppender] | type[LabFillBusLaneLedgerAppender],
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("# Ledger\n")
    handler = HandlerWorkLedgerAppendEffect(
        LocalLedgerAppendCommand(
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; "
                "p = Path(sys.argv[1]); "
                "p.write_text(p.read_text() + sys.argv[3] + '\\n')",
                str(ledger),
            ]
        ),
        LocalLedgerFile(ledger),
        "test-ledger-host",
        writer_principals=["operator"],
        operator_principal="operator",
    )

    @contextlib.asynccontextmanager
    async def open_bus(**kwargs: object) -> AsyncIterator[ProtocolLabRunBus]:
        bus = EventBusInmemory(environment="local", group="principal-proof")
        await bus.start()
        host = WorkLedgerAppendHost(bus, handler, writer_principals=["operator"])
        await host.start()
        try:
            yield bus
        finally:
            await host.stop()
            await bus.close()

    monkeypatch.setattr(cli, "open_lab_run_bus", open_bus)
    monkeypatch.setattr(lane_bus, "open_lab_run_bus", open_bus)
    request_id = uuid4()
    rows = f"2026-10-01T12:00:00Z | OPERATOR-CONSENT | req={request_id} | test authority\n  continuation"
    argv = [
        "append",
        "--bus",
        "inmemory",
        "--principal",
        "operator",
        "--lane",
        "lab",
        "--host",
        "test-host",
        "--request-id",
        str(request_id),
    ]
    first = CliRunner().invoke(work_ledger_group, argv, input=rows)
    assert first.exit_code == 0, first.output
    receipt = json.loads(first.stdout)
    assert receipt["status"] == "accepted"
    assert receipt["principal"] == "operator"
    assert receipt["ledger_lines"] == [2]
    assert (
        ledger.read_text()
        == "# Ledger\n" + rows.replace(" | req=", " | principal=operator | req=") + "\n"
    )
    before = ledger.read_bytes()
    second = CliRunner().invoke(work_ledger_group, argv, input=rows)
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["status"] == "duplicate"
    assert ledger.read_bytes() == before
    # The production handoff adapter selects its writer from the resolved login.
    monkeypatch.setattr(lane_bus, "bound_lane_principal", lambda: "operator")
    handoff_id = uuid4()
    handoff_request = ModelWorkLedgerAppendRequest(
        request_id=handoff_id,
        rows=f"2026-10-01T12:00:00Z | MSG | req={handoff_id} | handoff",
        requested_by_lane="lab",
        requesting_host="test-host",
        requested_at=datetime.now(UTC),
    )
    handoff_receipt = asyncio.run(
        appender_type("dev", tmp_path).append(handoff_request, timeout_s=2)
    )
    assert handoff_receipt is not None
    assert handoff_receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert handoff_receipt.principal == "operator"
    assert " | principal=operator | " in ledger.read_text().splitlines()[-1]


def test_append_rejects_payload_principal_for_kafka() -> None:
    result = CliRunner().invoke(
        work_ledger_group,
        [
            "append",
            "--bus",
            "kafka",
            "--principal",
            "x",
            "--lane",
            "lab",
            "--host",
            "host",
            "--request-id",
            str(uuid4()),
        ],
        input="rows",
    )
    assert result.exit_code == 2
    assert "the broker login is the writer identity" in result.stderr


def test_append_requires_principal_for_inmemory() -> None:
    result = CliRunner().invoke(
        work_ledger_group,
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
        ],
        input="rows",
    )
    assert result.exit_code == 2
    assert "requires --principal" in result.stderr


def test_serve_requires_writer_principal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ONEX_WORK_LEDGER_WRITER_PRINCIPALS")
    result = CliRunner().invoke(
        work_ledger_group,
        [
            "serve",
            "--host-name",
            "ledger",
            "--ledger",
            str(tmp_path / "ledger.md"),
            "--append-command",
            "true",
        ],
    )
    assert result.exit_code == 2
    assert "serve requires at least one --writer-principal" in result.stderr
    assert not (tmp_path / "ledger.md.work-ledger-serve.lock").exists()


@pytest.mark.parametrize("option", ["--writer-principal", "--operator-principal"])
def test_serve_rejects_invalid_principal(tmp_path: Path, option: str) -> None:
    result = CliRunner().invoke(
        work_ledger_group,
        [
            "serve",
            "--host-name",
            "ledger",
            "--ledger",
            str(tmp_path / "ledger.md"),
            "--append-command",
            "true",
            option,
            "Bad.Name",
        ],
    )
    assert result.exit_code == 2
    assert "invalid writer principal" in result.stderr


def test_writer_principal_callback_flattens_env_and_repeated_values() -> None:
    assert cli._writer_principals(
        None, None, ("dev-one,dev-two", "dev-three dev-four")
    ) == ("dev-one", "dev-two", "dev-three", "dev-four")


@pytest.mark.parametrize(
    ("environment", "arguments", "expected"),
    [
        (
            "dev-one,dev-two\ndev-three dev-four",
            [],
            ("dev-one", "dev-two", "dev-three", "dev-four"),
        ),
        (
            None,
            [
                "--writer-principal",
                "dev-one,dev-two",
                "--writer-principal",
                "dev-three",
            ],
            ("dev-one", "dev-two", "dev-three"),
        ),
    ],
)
def test_serve_resolves_writer_list_and_optional_operator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment: str | None,
    arguments: list[str],
    expected: tuple[str, ...],
) -> None:
    if environment is None:
        monkeypatch.delenv("ONEX_WORK_LEDGER_WRITER_PRINCIPALS")
    else:
        monkeypatch.setenv("ONEX_WORK_LEDGER_WRITER_PRINCIPALS", environment)
    monkeypatch.delenv("ONEX_WORK_LEDGER_OPERATOR_PRINCIPAL")
    identities: list[tuple[tuple[str, ...], str | None]] = []

    def handler(*args: object, **kwargs: object) -> HandlerWorkLedgerAppendEffect:
        identities.append((kwargs["writer_principals"], kwargs["operator_principal"]))
        return HandlerWorkLedgerAppendEffect()

    class UnavailableBus:
        async def __aenter__(self) -> ProtocolLabRunBus:
            raise LabRunBusError("bus unavailable after handler configuration")

        async def __aexit__(self, *exc: object) -> None:
            pass

    def unavailable(**kwargs: object) -> UnavailableBus:
        return UnavailableBus()

    monkeypatch.setattr(cli, "HandlerWorkLedgerAppendEffect", handler)
    monkeypatch.setattr(cli, "open_lab_run_bus", unavailable)
    result = CliRunner().invoke(
        work_ledger_group,
        [
            "serve",
            "--host-name",
            "ledger",
            "--ledger",
            str(tmp_path / "ledger.md"),
            "--append-command",
            "true",
            *arguments,
        ],
    )
    assert result.exit_code == 69, result.output
    assert identities == [(expected, None)]
