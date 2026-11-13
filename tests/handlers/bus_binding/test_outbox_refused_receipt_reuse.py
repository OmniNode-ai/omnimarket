# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A replayed request id that already has a stored clash refusal reuses that receipt instead of resending.

The outbox keeps a refused request under ``refused/`` with the request's rows and the refusal's receipt. A
replay of the same request id whose rows match a stored refusal whose message carries the clash mark is
counted as refused without calling the sender, without writing a second refused file, and without reporting
it to the writers a second time. A refusal for another cause, or corrected rows under the refused id, are
still sent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.bus_binding import outbox, recovery
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
    create_handler_exception_queue,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionQueueRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ModelAppendCommandResult,
)
from omnimarket.work_ledger_bus.bus import (
    WorkLedgerAppendCaller,
    WorkLedgerAppendHost,
)

pytestmark = pytest.mark.unit

REQUEST_ID = UUID("3f1c2a9e-5b7d-4c1e-8a2f-6d4e9b0c7a11")
WRITER = "writer-a"


class _FakeLedger:
    """The ledger file as text, with the append command and the reader the handler takes."""

    def __init__(self) -> None:
        self.text = ""

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.text += rows if rows.endswith("\n") else rows + "\n"
        return ModelAppendCommandResult(exit_code=0)

    def read_text(self) -> str:
        return self.text


def _request(
    words: str, *, request_id: UUID = REQUEST_ID
) -> ModelWorkLedgerAppendRequest:
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-11-02T02:41:00Z | FRICTION | lane=writer-lane | req={request_id} | {words}\n",
        requested_by_lane="writer-lane",
        requesting_host="h-test",
        requested_at=datetime(2026, 11, 2, 2, 41, tzinfo=UTC),
    )


@pytest.fixture
def ledger() -> _FakeLedger:
    return _FakeLedger()


@pytest.fixture
def handler(ledger: _FakeLedger) -> HandlerWorkLedgerAppendEffect:
    return HandlerWorkLedgerAppendEffect(
        runner=ledger,
        reader=ledger,
        host_name="ledger-host",
        writer_principals=[WRITER],
    )


def _counting_sender(
    handler: HandlerWorkLedgerAppendEffect,
) -> tuple[outbox.Sender, list[int]]:
    calls: list[int] = []

    async def send(
        request: ModelWorkLedgerAppendRequest,
    ) -> ModelWorkLedgerAppendReceipt:
        calls.append(1)
        return handler.handle(request, writer_principal=WRITER)

    return send, calls


def _refused_files(state_dir: Path) -> list[Path]:
    directory = outbox.outbox_dir(state_dir) / outbox.REFUSED_DIRNAME
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def _open_refused_rows(state_dir: Path) -> list[dict[str, object]]:
    rows = (
        create_handler_exception_queue()
        .handle(
            ModelExceptionQueueRequest(state_dir / "exception-queue", "orchestrator")
        )
        .rows
    )
    return [row for row in rows if row["cls"] == recovery.REFUSED_CLASS]


async def test_replay_of_clash_refusal_reuses_stored_receipt(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """A replay of a stored clash refusal is refused from the stored receipt, not resent."""
    landed = _request("the row that landed first")
    handler.handle(landed, writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)

    send, calls = _counting_sender(handler)
    result = await outbox.drain(tmp_path, send)
    assert len(calls) == 1
    assert result.refused == 1
    assert len(_refused_files(tmp_path)) == 1

    outbox.queue_request(tmp_path, queued)
    result = await outbox.drain(tmp_path, send)
    assert len(calls) == 1
    assert result.refused == 1
    assert len(_refused_files(tmp_path)) == 1
    assert outbox.pending_count(tmp_path) == 0

    now = datetime.now(UTC)
    recovery.report_refused_to_writers(tmp_path, now)
    recovery.report_refused_to_writers(tmp_path, now)
    assert len(_open_refused_rows(tmp_path)) == 1


async def test_replay_of_refusal_for_other_cause_is_still_sent(
    tmp_path: Path, ledger: _FakeLedger
) -> None:
    """A refusal that is not a clash is resent on the next drain."""
    outbox.queue_request(tmp_path, _request("a row the guard refused"))
    calls: list[int] = []

    async def send(
        request: ModelWorkLedgerAppendRequest,
    ) -> ModelWorkLedgerAppendReceipt:
        calls.append(1)
        return ModelWorkLedgerAppendReceipt(
            request_id=request.request_id,
            status=EnumWorkLedgerAppendStatus.REFUSED,
            exit_code=65,
            message="ledger_lock: OMN-18274 friction guard rejected append",
            ledger_lines=[],
            ledger_host="h-test",
            duration_ms=3,
        )

    result = await outbox.drain(tmp_path, send)
    assert len(calls) == 1
    assert result.refused == 1

    outbox.queue_request(tmp_path, _request("a row the guard refused"))
    result = await outbox.drain(tmp_path, send)
    assert len(calls) == 2
    assert result.refused == 1


async def test_corrected_rows_under_refused_id_are_still_sent(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """Corrected rows under a refused request id are sent again, not reused."""
    landed = _request("the row that landed first")
    handler.handle(landed, writer_principal=WRITER)
    outbox.queue_request(
        tmp_path, _request("a different row under the same request id")
    )

    send, calls = _counting_sender(handler)
    result = await outbox.drain(tmp_path, send)
    assert len(calls) == 1
    assert result.refused == 1

    outbox.queue_request(
        tmp_path, _request("the corrected row under the same request id")
    )
    result = await outbox.drain(tmp_path, send)
    assert len(calls) == 2
    assert result.refused == 1


async def test_seam_real_handler_clash_message_is_recognised(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """The real handler's clash refusal carries the mark the outbox recognises."""
    landed = _request("the row that landed first")
    handler.handle(landed, writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)

    receipt = handler.handle(queued, writer_principal=WRITER)
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert outbox.CLASH_MARK in receipt.message

    send, calls = _counting_sender(handler)
    result = await outbox.drain(tmp_path, send)
    assert len(calls) == 1
    assert result.refused == 1

    stored = outbox.stored_refusal(tmp_path, queued)
    assert stored is not None
    assert stored.message == receipt.message


async def test_seam_clash_travels_the_in_memory_bus_and_is_reused(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect
) -> None:
    """The append host answers on the append-completed topic; the outbox reads that receipt and reuses it.

    The caller and the host run on the in-memory bus the way the CLI's durable binding runs them on the broker, so a
    change to the receipt's shape on that topic, or to the host's clash message, fails here.
    """
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)

    bus = EventBusInmemory(environment="local", group="outbox-seam-test")
    await bus.start()
    host = WorkLedgerAppendHost(
        bus, handler, writer_principals=[WRITER], operator_principal="operator"
    )
    caller = WorkLedgerAppendCaller(bus, principal=WRITER)
    calls: list[UUID] = []

    async def send(
        request: ModelWorkLedgerAppendRequest,
    ) -> ModelWorkLedgerAppendReceipt:
        calls.append(request.request_id)
        return await caller.append(request, timeout_s=5)

    await host.start()
    try:
        first = await outbox.drain(tmp_path, send)
        outbox.queue_request(tmp_path, queued)
        second = await outbox.drain(tmp_path, send)
    finally:
        await caller.stop()
        await host.stop()
        await bus.close()

    assert calls == [REQUEST_ID]
    assert (first.refused, second.refused) == (1, 1)
    stored = outbox.stored_refusal(tmp_path, queued)
    assert stored is not None
    assert outbox.CLASH_MARK in stored.message
    assert len(_refused_files(tmp_path)) == 1
