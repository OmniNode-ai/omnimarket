# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23154: the serve stops appending to the file in a fenced flip, and its rollback never recreates the file.

AC1 ``flip_no_file_append``: after the flip a probe append returns a receipt with a ledger_seq and no file
line, and the reader verb reads the row back. AC2 ``fenced_handover_in_flight``: a command that arrives
during the handover gets a retry receipt, and the append already in flight lands once. AC3
``rollback_without_file_recreate``: the rollback restarts the file judge on the file as it stands, never
writes it, and never recreates it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omnimarket.models.work_ledger_append import EnumLedgerAppendMode
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendReceipt,
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
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import EnumReaderStatus
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
    SEAM_NOW,
    QueryNodePointRead,
    reader,
    rows_request,
)

pytestmark = pytest.mark.unit

PRIOR_ROW = "2026-11-09T10:00:00Z | STATUS | lane=old | ticket=OMN-1 | req=00000000-0000-4000-8000-000000000001 | earlier row"


class _FileRunner:
    """The file judge's append: writes the rows to the ledger file, and can hold one append in flight."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(rows + "\n")
        return ModelAppendCommandResult(exit_code=0)


def _ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text(PRIOR_ROW + "\n", encoding="utf-8")
    return path


def _serve(
    path: Path, runner: _FileRunner, store: InMemoryBusLedgerStore | None = None
) -> HandlerWorkLedgerAppendEffect:
    return HandlerWorkLedgerAppendEffect(
        runner,
        LocalLedgerFile(path),
        "ledger",
        writer_principals=("developer",),
        operator_principal="operator",
        fence=LedgerFlipFence(),
        bus_store=store if store is not None else InMemoryBusLedgerStore(head=1),
    )


def _request(
    request_id: UUID | None = None, text: str = "probe"
) -> ModelWorkLedgerAppendRequest:
    request_id = request_id or uuid4()
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-11-09T12:00:00Z | STATUS | lane=probe | ticket=OMN-23154 | req={request_id} | {text}",
        requested_by_lane="lab",
        requesting_host="lab-host",
        requested_at=datetime.now(UTC),
    )


def _send(
    serve: HandlerWorkLedgerAppendEffect, request: ModelWorkLedgerAppendRequest
) -> ModelWorkLedgerAppendReceipt:
    return serve.handle(request, writer_principal="developer")


def _in_file(path: Path, request: ModelWorkLedgerAppendRequest) -> int:
    return path.read_text(encoding="utf-8").count(f"req={request.request_id}")


def test_flip_no_file_append_returns_seq_and_the_reader_reads_it_back(
    tmp_path: Path,
) -> None:
    path = _ledger(tmp_path)
    runner = _FileRunner(path)
    store = InMemoryBusLedgerStore(head=1)
    serve = _serve(path, runner, store)

    before_flip = _send(serve, _request(text="before the flip"))
    assert before_flip.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert before_flip.ledger_lines == [2]
    assert before_flip.ledger_seq is None
    assert "ledger_seq" not in before_flip.model_dump(mode="json")

    flip = serve.flip_to_bus(drain_timeout_s=5)
    assert flip.flipped is True
    assert serve.mode is EnumLedgerAppendMode.BUS
    file_at_flip = path.read_bytes()

    probe = _request(text="probe after the flip")
    receipt = _send(serve, probe)
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.ledger_seq == 2
    assert receipt.ledger_lines == []
    assert runner.calls == 1  # the one append before the flip
    assert path.read_bytes() == file_at_flip
    assert _in_file(path, probe) == 0

    # The node verb: the bus store's rows folded into the query node, read through the reader verb.
    projection = InMemoryWorkLedgerQueryReader(now=lambda: SEAM_NOW)
    for seq, row in store.rows_by_seq():
        assert projection.ingest({"row_id": f"seq-{seq}", "raw_row": row})
    read = QueryNodePointRead(
        HandlerWorkLedgerQuery(reader=projection, now=lambda: SEAM_NOW)
    )
    answer = reader(read, clock=SEAM_NOW).handle(rows_request(window_hours=27))
    assert answer.status is EnumReaderStatus.OK, answer.reason
    assert any(f"req={probe.request_id}" in line for line in answer.lines)
    assert answer.file_read is False


def test_flip_no_file_append_with_the_file_gone_never_creates_it(
    tmp_path: Path,
) -> None:
    path = _ledger(tmp_path)
    runner = _FileRunner(path)
    serve = _serve(path, runner)
    assert serve.flip_to_bus(drain_timeout_s=5).flipped is True
    path.unlink()

    receipt = _send(serve, _request())
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.ledger_seq == 2
    assert runner.calls == 0
    assert not path.exists()


def test_flip_no_file_append_is_off_until_a_bus_store_is_bound(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    serve = HandlerWorkLedgerAppendEffect(
        _FileRunner(path),
        LocalLedgerFile(path),
        "ledger",
        writer_principals=("developer",),
        operator_principal="operator",
    )
    flip = serve.flip_to_bus(drain_timeout_s=1)
    assert flip.flipped is False
    assert serve.mode is EnumLedgerAppendMode.FILE
    assert _send(serve, _request()).status is EnumWorkLedgerAppendStatus.ACCEPTED


def _wait_for(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.005)


def test_fenced_handover_in_flight_retries_new_commands_and_lands_the_in_flight_once(
    tmp_path: Path,
) -> None:
    path = _ledger(tmp_path)
    runner = _FileRunner(path)
    runner.gate = threading.Event()
    store = InMemoryBusLedgerStore(head=1)
    serve = _serve(path, runner, store)

    in_flight = _request(text="in flight at the handover")
    in_flight_receipts: list[ModelWorkLedgerAppendReceipt] = []
    appender = threading.Thread(
        target=lambda: in_flight_receipts.append(_send(serve, in_flight))
    )
    appender.start()
    assert runner.entered.wait(5)

    flips = []
    flipper = threading.Thread(target=lambda: flips.append(serve.flip_to_bus(10)))
    flipper.start()
    _wait_for(lambda: serve.mode is EnumLedgerAppendMode.FENCED)

    arrived = _request(text="arrives during the handover")
    retry = _send(serve, arrived)
    assert retry.status is EnumWorkLedgerAppendStatus.RETRY
    assert retry.exit_code == 75
    assert retry.ledger_lines == []
    assert retry.ledger_seq is None
    assert runner.calls == 1
    assert _in_file(path, arrived) == 0
    assert store.lookup(arrived.request_id) is None
    assert flipper.is_alive()  # the handover waits for the append in flight

    runner.gate.set()
    appender.join(10)
    flipper.join(10)
    assert in_flight_receipts[0].status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert flips[0].flipped is True
    assert flips[0].in_flight_at_start == 1
    assert serve.mode is EnumLedgerAppendMode.BUS
    assert _in_file(path, in_flight) == 1

    # The command that was told to retry now lands on the bus, once.
    retried = _send(serve, arrived)
    assert retried.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert retried.ledger_seq == 2
    assert _in_file(path, arrived) == 0

    # Nothing is doubled: the in-flight request answers from the file, the retried one from the bus.
    replay_in_flight = _send(serve, in_flight)
    assert replay_in_flight.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert replay_in_flight.ledger_lines == in_flight_receipts[0].ledger_lines
    assert store.lookup(in_flight.request_id) is None
    replay_retried = _send(serve, arrived)
    assert replay_retried.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert replay_retried.ledger_seq == 2
    assert store.text().count(f"req={arrived.request_id}") == 1
    assert runner.calls == 1


def test_fenced_handover_in_flight_that_does_not_drain_leaves_the_serve_on_the_file(
    tmp_path: Path,
) -> None:
    path = _ledger(tmp_path)
    runner = _FileRunner(path)
    runner.gate = threading.Event()
    serve = _serve(path, runner)
    stuck = _request(text="held in flight")
    appender = threading.Thread(target=lambda: _send(serve, stuck))
    appender.start()
    assert runner.entered.wait(5)

    flip = serve.flip_to_bus(drain_timeout_s=0.05)
    assert flip.flipped is False
    assert flip.in_flight_at_start == 1
    assert serve.mode is EnumLedgerAppendMode.FILE

    runner.gate.set()
    appender.join(10)
    runner.gate = None
    later = _request(text="after the failed flip")
    assert _send(serve, later).status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert _in_file(path, stuck) == 1
    assert _in_file(path, later) == 1


def test_rollback_without_file_recreate_restarts_the_file_judge_on_the_file_as_it_stands(
    tmp_path: Path,
) -> None:
    path = _ledger(tmp_path)
    runner = _FileRunner(path)
    store = InMemoryBusLedgerStore(head=1)
    serve = _serve(path, runner, store)
    assert serve.flip_to_bus(drain_timeout_s=5).flipped is True
    file_at_flip = path.read_bytes()
    on_bus = [_request(text=f"bus row {n}") for n in range(2)]
    for request in on_bus:
        assert _send(serve, request).ledger_seq is not None

    result = serve.rollback_to_file(drain_timeout_s=5)
    assert result.restored is True
    assert result.mode is EnumLedgerAppendMode.FILE
    assert result.bus_rows_not_in_file == 2
    assert result.file_unchanged_since_flip is True
    assert path.read_bytes() == file_at_flip  # no export of the bus rows
    assert all(_in_file(path, request) == 0 for request in on_bus)

    # The old judge is back: a new command lands in the file again.
    after = _request(text="after the rollback")
    receipt = _send(serve, after)
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.ledger_lines == [2]
    assert receipt.ledger_seq is None
    assert runner.calls == 1

    # A request the bus already took is answered from the bus, never appended to the file as well.
    replay = _send(serve, on_bus[0])
    assert replay.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert replay.ledger_seq == 2
    assert _in_file(path, on_bus[0]) == 0
    assert runner.calls == 1


def test_rollback_without_file_recreate_refuses_when_the_file_is_gone(
    tmp_path: Path,
) -> None:
    path = _ledger(tmp_path)
    runner = _FileRunner(path)
    serve = _serve(path, runner)
    assert serve.flip_to_bus(drain_timeout_s=5).flipped is True
    path.unlink()
    assert _send(serve, _request()).ledger_seq == 2

    result = serve.rollback_to_file(drain_timeout_s=5)
    assert result.restored is False
    assert result.mode is EnumLedgerAppendMode.BUS
    assert "never recreates" in result.reason
    assert not path.exists()
    assert serve.mode is EnumLedgerAppendMode.BUS
    assert _send(serve, _request()).ledger_seq == 3
    assert not path.exists()
    assert runner.calls == 0


def test_rollback_without_file_recreate_reports_a_file_changed_since_the_flip(
    tmp_path: Path,
) -> None:
    path = _ledger(tmp_path)
    serve = _serve(path, _FileRunner(path))
    assert serve.flip_to_bus(drain_timeout_s=5).flipped is True
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            "2026-11-09T11:00:00Z | STATUS | lane=other | ticket=OMN-1 | hand edit\n"
        )
    result = serve.rollback_to_file(drain_timeout_s=5)
    assert result.restored is True
    assert result.file_unchanged_since_flip is False
