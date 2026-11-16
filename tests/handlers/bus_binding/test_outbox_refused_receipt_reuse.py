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

import fcntl
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.bus_binding import outbox, recovery, select
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
from tests.unit.bus_binding.test_bus_binding_fallback import LANE, _Switch
from tests.unit.bus_binding.test_bus_binding_fallback import (
    workspace as workspace,
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


async def test_stored_refusal_lookup_leaves_still_sent_cases_unreused(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """Only a clash refusal over the same rows is reused; a guard refusal and changed rows are still sent."""
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, _calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)

    assert outbox.stored_refusal(tmp_path, queued) is not None
    assert (
        outbox.stored_refusal(
            tmp_path, _request("the corrected row under the same request id")
        )
        is None
    )

    guard_dir = tmp_path / "guard"
    guard_request = _request("a row the guard refused", request_id=UUID(int=7))
    outbox.queue_request(guard_dir, guard_request)

    async def guard_send(
        request: ModelWorkLedgerAppendRequest,
    ) -> ModelWorkLedgerAppendReceipt:
        return ModelWorkLedgerAppendReceipt(
            request_id=request.request_id,
            status=EnumWorkLedgerAppendStatus.REFUSED,
            exit_code=65,
            message="ledger_lock: OMN-18274 friction guard rejected append",
            ledger_lines=[],
            ledger_host="h-test",
            duration_ms=3,
        )

    await outbox.drain(guard_dir, guard_send)
    assert len(_refused_files(guard_dir)) == 1
    assert outbox.stored_refusal(guard_dir, guard_request) is None


async def test_replay_of_clash_refusal_adds_no_new_report_for_the_writer(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """After a clash refusal was reported once, replaying it adds nothing new to report."""
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, _calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)
    assert recovery.report_refused_to_writers(tmp_path, datetime.now(UTC)) == 1

    for _ in range(3):
        outbox.queue_request(tmp_path, queued)
        await outbox.drain(tmp_path, send)
        assert recovery.report_refused_to_writers(tmp_path, datetime.now(UTC)) == 0
    assert len(_open_refused_rows(tmp_path)) == 1


def _refused_body(state_dir: Path) -> dict[str, object]:
    (path,) = _refused_files(state_dir)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(body, dict)
    return body


@pytest.mark.parametrize("corrupt", ["[]", "null", '"text"', "7", "{not json"])
async def test_outbox_corrupt_refused_file_is_left_out_and_the_drain_returns(
    tmp_path: Path,
    handler: HandlerWorkLedgerAppendEffect,
    ledger: _FakeLedger,
    corrupt: str,
) -> None:
    """A refused file that is not a JSON object is left out of the lookup: the request is sent, the drain returns."""
    queued = _request("a row whose refused file is corrupt")
    outbox.queue_request(tmp_path, queued)
    refused = outbox.outbox_dir(tmp_path) / outbox.REFUSED_DIRNAME
    refused.mkdir()
    (refused / f"00000000000000000001_{queued.request_id}.json").write_text(
        corrupt, encoding="utf-8"
    )

    assert outbox.stored_refusal(tmp_path, queued) is None
    send, calls = _counting_sender(handler)
    result = await outbox.drain(tmp_path, send)
    assert len(calls) == 1
    assert result.replayed == 1
    assert result.stopped is None
    assert outbox.pending_count(tmp_path) == 0


async def test_outbox_reuse_logs_one_line_per_reuse(
    tmp_path: Path,
    handler: HandlerWorkLedgerAppendEffect,
    ledger: _FakeLedger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each reuse of a stored refusal is logged once, with the request id."""
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, _calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)

    with caplog.at_level(logging.INFO, logger=outbox.__name__):
        for _ in range(2):
            outbox.queue_request(tmp_path, queued)
            await outbox.drain(tmp_path, send)
    lines = [r.getMessage() for r in caplog.records if r.name == outbox.__name__]
    assert len(lines) == 2
    assert all(str(queued.request_id) in line and "reus" in line for line in lines)


async def test_outbox_reuse_settles_the_attempt_entry(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """A reused refusal ends the row's retries: its attempt entry is forgotten."""
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, _calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)

    outbox.queue_request(tmp_path, queued)
    outbox.first_attempt(tmp_path, queued)
    attempts = outbox.outbox_dir(tmp_path) / outbox.ATTEMPTS_DIRNAME
    assert list(attempts.glob("*.json"))
    result = await outbox.drain(tmp_path, send)
    assert result.refused == 1
    assert not list(attempts.glob("*.json"))


async def test_outbox_reuse_mixed_batch_still_sends_the_sendable_request(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """A reused request does not hold back the sendable request queued after it."""
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    clash = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, clash)
    send, calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)
    assert len(calls) == 1

    fresh = _request("an unrelated row", request_id=UUID(int=11))
    outbox.queue_request(tmp_path, clash)
    outbox.queue_request(tmp_path, fresh)
    result = await outbox.drain(tmp_path, send)
    assert (result.refused, result.replayed) == (1, 1)
    assert len(calls) == 2
    assert ledger.text.count("an unrelated row") == 1
    assert outbox.pending_count(tmp_path) == 0


async def test_outbox_reuse_bound_by_age_sends_again_after_the_window(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """Past the window the stored refusal is not reused: the ledger can have rolled the other rows away.

    The append handler reads only the live ledger file (and the bus store), so after a roll to archive the id
    is unknown to it and the same rows are accepted; a reuse that never expired would drop them for good.
    """
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)
    assert len(calls) == 1
    assert _refused_body(tmp_path)["refused_at"]

    # within the window the refusal is reused
    assert outbox.stored_refusal(tmp_path, queued) is not None
    # past the window it is not
    later = datetime.now(UTC) + outbox.REUSE_WINDOW + timedelta(minutes=1)
    assert outbox.stored_refusal(tmp_path, queued, now=later) is None

    # the ledger rolls: the live file no longer holds the first row
    (path,) = _refused_files(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["refused_at"] = (
        datetime.now(UTC) - outbox.REUSE_WINDOW - timedelta(minutes=1)
    ).isoformat()
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    ledger.text = ""
    outbox.queue_request(tmp_path, queued)
    result = await outbox.drain(tmp_path, send)
    assert len(calls) == 2
    assert (result.replayed, result.refused) == (1, 0)
    assert "a different row under the same request id" in ledger.text


async def test_outbox_reuse_bound_falls_back_to_the_file_name_stamp(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """A refused file from before the change carries no refused_at; its queue stamp bounds it."""
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, _calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)

    (path,) = _refused_files(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    del body["refused_at"]
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    assert outbox.stored_refusal(tmp_path, queued) is not None

    old = path.with_name(f"00000000000000000001_{queued.request_id}.json")
    path.rename(old)
    assert outbox.stored_refusal(tmp_path, queued) is None


async def test_outbox_reuse_through_the_wired_caller_raises_no_occurrence(
    workspace: Path,
    tmp_path: Path,
    handler: HandlerWorkLedgerAppendEffect,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replay through select.replay_outbox, the caller the CLI runs, adds no exception-queue occurrence.

    The caller reports from the files in refused/ (report_refused_to_writers), never from DrainResult.refused,
    so a reuse that writes no second file emits nothing. The emit handler is counted at its seam.
    """
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    state = tmp_path / "state"
    send, calls = _counting_sender(handler)
    emitted: list[object] = []
    real_emit = recovery.create_handler_exception_emit

    def counting_emit() -> Any:
        inner = real_emit()

        class _Counting:
            def handle(self, request: Any) -> Any:
                emitted.append(request)
                return inner.handle(request)

        return _Counting()

    monkeypatch.setattr(recovery, "create_handler_exception_emit", counting_emit)

    async def replay() -> outbox.DrainResult:
        switch = _Switch()
        switch.up = True
        async with select.open_bound_bus(
            bus="kafka",
            lane=LANE,
            kafka_bootstrap=None,
            workspace_root=workspace,
            state_dir=state,
            open_bus=switch,
            announce=lambda _: None,
            pending_outbox=lambda: outbox.pending_count(state),
        ) as bound:
            return await select.replay_outbox(bound, state, send)

    outbox.queue_request(state, queued)
    first = await replay()
    assert first.refused == 1
    assert len(emitted) == 1
    assert len(_open_refused_rows(state)) == 1

    outbox.queue_request(state, queued)
    second = await replay()
    assert second.refused == 1
    assert len(calls) == 1
    assert len(emitted) == 1
    assert len(_open_refused_rows(state)) == 1


def _age_refused_file(state_dir: Path, *, age: timedelta) -> Path:
    """Stamp the one refused file as written ``age`` ago, the way a roll-aged refusal reads."""
    (path,) = _refused_files(state_dir)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["refused_at"] = (datetime.now(UTC) - age).isoformat()
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    return path


async def test_outbox_refused_window_repeat_clash_refreshes_the_one_file(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """Past the window, with the id still in the ledger, the replay is sent again and leaves one refused file.

    The clash is refused again over the same rows, so the stored file's ``refused_at`` is refreshed in place:
    no second file is written, the writer is not told a second time, and the reuse window restarts.
    """
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)
    assert recovery.report_refused_to_writers(tmp_path, datetime.now(UTC)) == 1

    aged = _age_refused_file(tmp_path, age=outbox.REUSE_WINDOW + timedelta(minutes=1))
    aged_at = datetime.fromisoformat(str(_refused_body(tmp_path)["refused_at"]))
    outbox.queue_request(tmp_path, queued)
    result = await outbox.drain(tmp_path, send)

    assert len(calls) == 2
    assert result.refused == 1
    assert _refused_files(tmp_path) == [aged]
    body = _refused_body(tmp_path)
    assert datetime.fromisoformat(str(body["refused_at"])) > aged_at
    assert body["reported_at"]
    assert recovery.report_refused_to_writers(tmp_path, datetime.now(UTC)) == 0
    assert len(_open_refused_rows(tmp_path)) == 1

    outbox.queue_request(tmp_path, queued)
    await outbox.drain(tmp_path, send)
    assert len(calls) == 2


async def test_outbox_refused_window_pre_change_file_is_refreshed_not_duplicated(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """A refused file from before the change has no ``refused_at``; its queue stamp is old, so the replay is
    sent once, and the refusal that comes back is written into that file, not into a second one."""
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)
    assert recovery.report_refused_to_writers(tmp_path, datetime.now(UTC)) == 1

    (path,) = _refused_files(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    del body["refused_at"]
    old = path.with_name(f"00000000000000000001_{queued.request_id}.json")
    path.unlink()
    old.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")

    outbox.queue_request(tmp_path, queued)
    await outbox.drain(tmp_path, send)
    assert len(calls) == 2
    assert _refused_files(tmp_path) == [old]
    assert _refused_body(tmp_path)["refused_at"]
    assert recovery.report_refused_to_writers(tmp_path, datetime.now(UTC)) == 0
    assert len(_open_refused_rows(tmp_path)) == 1


async def test_outbox_refused_window_changed_rows_still_write_their_own_file(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """Only a repeat over the same rows is refreshed in place; other rows under the id are a new refusal."""
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    outbox.queue_request(
        tmp_path, _request("a different row under the same request id")
    )
    send, _calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)

    outbox.queue_request(
        tmp_path, _request("yet another row under the same request id")
    )
    await outbox.drain(tmp_path, send)
    assert len(_refused_files(tmp_path)) == 2


async def _stored_with_refused_at(
    tmp_path: Path,
    handler: HandlerWorkLedgerAppendEffect,
    stamp: object,
) -> tuple[ModelWorkLedgerAppendRequest, Path]:
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, _calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)
    (path,) = _refused_files(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["refused_at"] = stamp
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    return queued, path


async def test_outbox_refused_at_in_the_future_is_expired(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """A stamp after the clock reads as expired, not as fresh for the whole window and then some."""
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    queued, _path = await _stored_with_refused_at(tmp_path, handler, future)
    assert outbox.stored_refusal(tmp_path, queued) is None

    send, calls = _counting_sender(handler)
    outbox.queue_request(tmp_path, queued)
    await outbox.drain(tmp_path, send)
    assert len(calls) == 1
    assert len(_refused_files(tmp_path)) == 1
    assert datetime.fromisoformat(str(_refused_body(tmp_path)["refused_at"])) <= (
        datetime.now(UTC)
    )


@pytest.mark.parametrize("stamp", ["not a date", "", None, 7, ["x"]])
async def test_outbox_refused_at_unparsable_is_expired(
    tmp_path: Path,
    handler: HandlerWorkLedgerAppendEffect,
    ledger: _FakeLedger,
    stamp: object,
) -> None:
    """A stamp that is present but unusable is expired; it does not fall back to a fresh queue stamp."""
    queued, _path = await _stored_with_refused_at(tmp_path, handler, stamp)
    assert outbox.stored_refusal(tmp_path, queued) is None


async def test_outbox_refused_at_naive_is_read_as_utc(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """A stamp with no zone is a UTC stamp: a recent one is reused, one past the window is not."""
    recent = datetime.now(UTC).replace(tzinfo=None).isoformat()
    queued, path = await _stored_with_refused_at(tmp_path, handler, recent)
    assert outbox.stored_refusal(tmp_path, queued) is not None

    body = json.loads(path.read_text(encoding="utf-8"))
    body["refused_at"] = (
        (datetime.now(UTC) - outbox.REUSE_WINDOW - timedelta(minutes=1))
        .replace(tzinfo=None)
        .isoformat()
    )
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    assert outbox.stored_refusal(tmp_path, queued) is None


async def _replay_through_select(
    workspace: Path, state: Path, send: outbox.Sender
) -> outbox.DrainResult:
    switch = _Switch()
    switch.up = True
    async with select.open_bound_bus(
        bus="kafka",
        lane=LANE,
        kafka_bootstrap=None,
        workspace_root=workspace,
        state_dir=state,
        open_bus=switch,
        announce=lambda _: None,
        pending_outbox=lambda: outbox.pending_count(state),
    ) as bound:
        return await select.replay_outbox(bound, state, send)


@pytest.mark.parametrize(
    ("corrupt", "reported"),
    [
        ("[]", 0),
        ("null", 0),
        ('"text"', 0),
        ("7", 0),
        ("{not json", 0),
        ('{"refusal": "text", "rows": "a row"}', 1),
        ('{"refusal": ["x"], "rows": ["a row"], "requested_by_lane": 3}', 1),
    ],
)
async def test_outbox_corrupt_refused_file_replay_through_select_survives(
    workspace: Path,
    tmp_path: Path,
    handler: HandlerWorkLedgerAppendEffect,
    ledger: _FakeLedger,
    corrupt: str,
    reported: int,
) -> None:
    """select.replay_outbox, the caller the CLI runs, survives a corrupt file in refused/ and leaves its text alone.

    The replay drains (the lookup leaves the file out) and then reports from the files in refused/
    (report_refused_to_writers), so both readers meet the file. A file that is not a JSON object is skipped by
    the report; an object whose refusal is not an object is reported with unreadable evidence, not raised on.
    """
    state = tmp_path / "state"
    queued = _request("a row whose refused file is corrupt")
    outbox.queue_request(state, queued)
    refused = outbox.outbox_dir(state) / outbox.REFUSED_DIRNAME
    refused.mkdir()
    bad = refused / f"00000000000000000001_{queued.request_id}.json"
    bad.write_text(corrupt, encoding="utf-8")
    send, calls = _counting_sender(handler)

    result = await _replay_through_select(workspace, state, send)

    assert len(calls) == 1
    assert (result.replayed, result.stopped) == (1, None)
    assert len(_open_refused_rows(state)) == reported
    if not reported:
        assert bad.read_text(encoding="utf-8") == corrupt
    else:
        assert "unreadable" in str(_open_refused_rows(state)[0]["evidence"])
    assert recovery.report_refused_to_writers(state, datetime.now(UTC)) == 0


async def test_outbox_reuse_log_line_is_shown_where_the_append_cli_logs(
    tmp_path: Path,
    handler: HandlerWorkLedgerAppendEffect,
    ledger: _FakeLedger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reuse line reaches a process that configures no logging.

    Only ``work-ledger serve`` calls ``logging.basicConfig``; the ``append`` command that runs the replay does
    not, so Python's last-resort handler shows WARNING and above. An INFO reuse line would be silent there and
    a dropped replay would leave no trace, so the line is a warning.
    """
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, _calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)

    with caplog.at_level(logging.WARNING, logger=outbox.__name__):
        outbox.queue_request(tmp_path, queued)
        await outbox.drain(tmp_path, send)
    lines = [r for r in caplog.records if r.name == outbox.__name__]
    assert len(lines) == 1
    assert lines[0].levelno >= logging.WARNING
    assert str(queued.request_id) in lines[0].getMessage()


async def test_outbox_refused_lock_reporter_leaves_the_pass_while_a_drain_holds_it(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """The reporter rewrites a refused file's whole body, as the drain's refresh does, so it takes the drain lock.

    While another process holds ``.drain.lock`` (a drain that may be mid-refresh) the reporter writes nothing:
    the file keeps no ``reported_at`` and no case is emitted. Once the lock is free the next pass reports it
    once, so a stamp written between a read and a replace is never lost and the writer is told once.
    """
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, _calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)
    before = _refused_body(tmp_path)
    assert "reported_at" not in before

    lock_path = outbox.outbox_dir(tmp_path) / outbox.DRAIN_LOCK_NAME
    with lock_path.open("a+", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert recovery.report_refused_to_writers(tmp_path, datetime.now(UTC)) == 0
        assert _refused_body(tmp_path) == before
        assert _open_refused_rows(tmp_path) == []

    assert recovery.report_refused_to_writers(tmp_path, datetime.now(UTC)) == 1
    assert _refused_body(tmp_path)["reported_at"]
    assert recovery.report_refused_to_writers(tmp_path, datetime.now(UTC)) == 0
    assert len(_open_refused_rows(tmp_path)) == 1


async def test_outbox_refused_lock_reporter_releases_the_lock_for_the_next_drain(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """The reporter holds the lock only for its pass: a drain right after it is not refused."""
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, _calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)
    assert recovery.report_refused_to_writers(tmp_path, datetime.now(UTC)) == 1

    outbox.queue_request(tmp_path, queued)
    result = await outbox.drain(tmp_path, send)
    assert result.stopped is None
    assert result.refused == 1


def _three_stored_clash_files(
    state_dir: Path, queued: ModelWorkLedgerAppendRequest, *, ages: list[timedelta]
) -> list[Path]:
    """Three refused files for one id and one set of rows, oldest queue stamp first.

    The first drain wrote one file; it is replaced by three copies under earlier queue stamps, each told
    apart by its ledger host and aged by ``ages`` (oldest first).
    """
    (written,) = _refused_files(state_dir)
    body = json.loads(written.read_text(encoding="utf-8"))
    paths: list[Path] = []
    for index, age in enumerate(ages):
        copy = json.loads(json.dumps(body))
        copy["refusal"]["ledger_host"] = f"stored-{index}"
        copy["refused_at"] = (datetime.now(UTC) - age).isoformat()
        copy.pop("reported_at", None)
        path = written.with_name(f"{index + 1:020d}_{queued.request_id}.json")
        path.write_text(json.dumps(copy, sort_keys=True), encoding="utf-8")
        paths.append(path)
    written.unlink()
    return paths


async def test_outbox_stored_clash_files_for_one_id_refresh_only_the_newest(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """Several stored clash files for one id and rows: the refresh rewrites the newest, the last sorted name.

    The files sort by their queue stamp, oldest first, and ``stored[-1]`` is the newest. A repeat clash past
    the window is written into that one, so the older copies keep their words and their ``refused_at``.
    """
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)
    expired = outbox.REUSE_WINDOW + timedelta(hours=1)
    oldest, middle, newest = _three_stored_clash_files(
        tmp_path,
        queued,
        ages=[expired + timedelta(hours=2), expired + timedelta(hours=1), expired],
    )
    untouched = {p: p.read_bytes() for p in (oldest, middle)}
    assert outbox.stored_refusal(tmp_path, queued) is None

    outbox.queue_request(tmp_path, queued)
    result = await outbox.drain(tmp_path, send)

    assert len(calls) == 2
    assert result.refused == 1
    assert _refused_files(tmp_path) == [oldest, middle, newest]
    assert {p: p.read_bytes() for p in (oldest, middle)} == untouched
    body = json.loads(newest.read_text(encoding="utf-8"))
    assert body["refusal"]["ledger_host"] != "stored-2"
    assert datetime.now(UTC) - datetime.fromisoformat(body["refused_at"]) < timedelta(
        minutes=1
    )


async def test_outbox_stored_clash_files_for_one_id_reuse_any_file_in_the_window(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """Of several stored clash files for one id and rows, one still inside the window answers the replay.

    Here only the oldest-named file is fresh (its ``refused_at`` was refreshed); the newer-named ones are
    past the window. The replay is answered from the stored refusal and nothing is sent or written.
    """
    handler.handle(_request("the row that landed first"), writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)
    send, calls = _counting_sender(handler)
    await outbox.drain(tmp_path, send)
    expired = outbox.REUSE_WINDOW + timedelta(hours=1)
    paths = _three_stored_clash_files(
        tmp_path, queued, ages=[timedelta(minutes=5), expired, expired]
    )
    before = {p: p.read_bytes() for p in paths}

    outbox.queue_request(tmp_path, queued)
    result = await outbox.drain(tmp_path, send)

    assert len(calls) == 1
    assert result.refused == 1
    assert {p: p.read_bytes() for p in _refused_files(tmp_path)} == before


def test_outbox_drain_callers_in_src_are_the_replay_only() -> None:
    """The one caller of ``outbox.drain`` in src/ is ``select.replay_outbox``, which reports from refused/.

    A new caller would read ``DrainResult.refused`` without the replay's report step, so it has to be looked
    at: this fails until the new caller is listed here.
    """
    src = Path(__file__).resolve().parents[3] / "src"
    callers = sorted(
        str(path.relative_to(src.parent))
        for path in src.rglob("*.py")
        if "outbox.drain(" in path.read_text(encoding="utf-8")
    )
    assert callers == ["src/omnimarket/handlers/bus_binding/select.py"]
