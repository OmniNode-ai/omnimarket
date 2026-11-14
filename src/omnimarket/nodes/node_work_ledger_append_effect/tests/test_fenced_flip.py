# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23154: the serve stops appending to the file in a fenced flip, and the rollback never recreates it.

Golden chains: after the flip a probe append returns a receipt with a ledger_seq and writes no file
line, and the query node reads the row back; commands arriving while the handover runs are answered
``retry`` and the in-flight append lands once; the rollback restarts the file judge from the
reducer's records and leaves an absent file absent. Error chains: a switch that fails leaves the
authority where it was with the fence lifted, and a flip whose file and reducer disagree about the
last row is refused.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.work_ledger_judge_acceptance import (
    InMemoryJudgeAcceptanceStore,
)
from omnimarket.models.work_ledger_append import (
    EnumLedgerAuthority,
    ModelFileJudgeRestartState,
)
from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
)
from omnimarket.nodes.node_event_emit_effect.handlers.inmemory_bus_emitter import (
    InMemoryBusEmitter,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.adapters.in_memory_ledger_authority import (
    InMemoryLedgerAuthority,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.work_ledger_flip_fence import (
    FenceDrainTimeoutError,
    FlipRefusedError,
)
from omnimarket.nodes.node_work_ledger_append_effect.local_serve import (
    load_fenced_flip_policy,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerFile,
    ModelAppendCommandResult,
)
from omnimarket.nodes.node_work_ledger_emit_effect import (
    HandlerWorkLedgerEmit,
    ModelWorkLedgerEmitRequest,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
FILE_SEQ_BASE = 190464


class _FileJudge:
    """The file judge's runner: appends to the ledger file, can be held mid-append, and counts calls."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.seqs: dict[str, int] = {}

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=10), "the test never released the held append"
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(rows if rows.endswith("\n") else rows + "\n")
        return ModelAppendCommandResult(exit_code=0)


class _FileSequences:
    """The file judge's gapless counter, read back by the handler after an append."""

    def __init__(self, judge: _FileJudge) -> None:
        self._judge = judge
        self._next = FILE_SEQ_BASE

    def sequence_of(self, row_id: str) -> int | None:
        return self._judge.seqs.setdefault(row_id, self._take())

    def _take(self) -> int:
        self._next += 1
        return self._next


class _Restart:
    def __init__(self, *, fail: bool = False) -> None:
        self.states: list[ModelFileJudgeRestartState] = []
        self.fail = fail

    def restart(self, state: ModelFileJudgeRestartState) -> None:
        if self.fail:
            raise RuntimeError("the file judge did not start")
        self.states.append(state)


def _handler(
    ledger: Path,
    authority: InMemoryLedgerAuthority,
    store: InMemoryJudgeAcceptanceStore,
    restart: _Restart | None = None,
    *,
    drain_wait_seconds: float | None = None,
) -> tuple[HandlerWorkLedgerAppendEffect, _FileJudge]:
    judge = _FileJudge(ledger)
    policy = load_fenced_flip_policy()
    if drain_wait_seconds is not None:
        policy = policy.model_copy(update={"drain_wait_seconds": drain_wait_seconds})
    handler = HandlerWorkLedgerAppendEffect(
        judge,
        LocalLedgerFile(ledger),
        "ledger",
        writer_principals=("developer",),
        operator_principal="operator",
        acceptance_recorder=store,
        sequence_source=_FileSequences(judge),
        fenced_flip=policy,
        authority=authority,
        file_judge_restart=restart or _Restart(),
    )
    return handler, judge


def _request(request_id: UUID, text: str = "probe") -> ModelWorkLedgerAppendRequest:
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-11-14T08:00:00Z | STATUS | lane=probe | ticket=OMN-23154 | req={request_id} | {text}",
        requested_by_lane="lab",
        requesting_host="lab-host",
        requested_at=NOW,
    )


def _append(
    handler: HandlerWorkLedgerAppendEffect, request: ModelWorkLedgerAppendRequest
) -> ModelWorkLedgerAppendReceipt:
    return handler.handle(request, writer_principal="developer")


def _file_row_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


async def test_flip_no_file_append_probe_returns_a_sequence_and_the_node_reads_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: after the flip no append reaches the file; a probe gets a ledger_seq and the node verb reads it."""
    # The emit guard refuses a test that targets a real broker; this one emits through an in-memory bus.
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    store = InMemoryJudgeAcceptanceStore()
    authority = InMemoryLedgerAuthority()
    handler, judge = _handler(ledger, authority, store)

    before_flip = handler.handle(
        _request(uuid4(), "before"), writer_principal="developer"
    )
    assert before_flip.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert before_flip.ledger_lines == [1]
    assert (
        before_flip.ledger_seqs == []
    )  # the file judge's receipt is unchanged on the wire
    assert "ledger_seqs" not in before_flip.model_dump(mode="json")
    file_bytes = ledger.read_bytes()
    assert judge.calls == 1

    flipped = handler.flip_to_reducer()
    assert flipped.authority is EnumLedgerAuthority.REDUCER
    assert flipped.fence_seq == FILE_SEQ_BASE + 1

    probe_id = uuid4()
    receipt = handler.handle(_request(probe_id), writer_principal="developer")
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.ledger_seqs == [FILE_SEQ_BASE + 2]
    assert receipt.ledger_lines == []
    assert receipt.model_dump(mode="json")["ledger_seqs"] == [FILE_SEQ_BASE + 2]
    assert ledger.read_bytes() == file_bytes, "a row reached the file after the flip"
    assert judge.calls == 1, "the file judge was called after the flip"

    # The row the reducer committed reaches the bus through the emit gate and the node verb reads it back.
    row = authority.rows()[-1]
    assert f"req={probe_id}" in row
    bus = EventBusInmemory()
    await bus.start()
    reader = InMemoryWorkLedgerQueryReader()
    await reader.attach(bus)
    emitted = await asyncio.to_thread(
        HandlerWorkLedgerEmit(
            emitter=InMemoryBusEmitter(bus), acceptance_reader=store
        ).handle,
        ModelWorkLedgerEmitRequest(row=row, ledger_seq=receipt.ledger_seqs[0]),
    )
    assert emitted.accepted is True
    answer = HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW).handle(
        ModelWorkLedgerQueryRequest(
            correlation_id=uuid4(),
            query=EnumWorkLedgerQueryKind.ROWS,
            now=NOW,
            ticket="OMN-23154",
        )
    )
    assert answer.error is None
    assert [r.text for r in answer.rows] == [row]


def test_flip_no_file_append_refuses_a_flip_when_file_and_reducer_name_different_rows(
    tmp_path: Path,
) -> None:
    """Error chain: the file holds a row the reducer never heard of, so nothing is switched."""
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "2026-11-14T07:00:00Z | STATUS | lane=other | req=x | unseen\n",
        encoding="utf-8",
    )
    handler, judge = _handler(
        ledger, InMemoryLedgerAuthority(), InMemoryJudgeAcceptanceStore()
    )
    with pytest.raises(FlipRefusedError):
        handler.flip_to_reducer()
    assert handler.authority is EnumLedgerAuthority.FILE
    assert handler.flip_fence is not None
    assert handler.flip_fence.closed is False
    receipt = handler.handle(_request(uuid4()), writer_principal="developer")
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert judge.calls == 1


def test_fenced_handover_in_flight_new_commands_retry_and_nothing_is_lost_or_doubled(
    tmp_path: Path,
) -> None:
    """AC2: a command during the handover gets a retry receipt; the in-flight append lands once."""
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    store = InMemoryJudgeAcceptanceStore()
    authority = InMemoryLedgerAuthority()
    handler, judge = _handler(ledger, authority, store)
    fence = handler.flip_fence
    assert fence is not None

    in_flight_id, new_id = uuid4(), uuid4()
    judge.release.clear()
    results: dict[str, Any] = {}
    writer = threading.Thread(
        target=lambda: results.__setitem__(
            "in_flight", _append(handler, _request(in_flight_id, "in flight"))
        )
    )
    writer.start()
    assert judge.started.wait(timeout=10)
    assert fence.in_flight == 1

    flipper = threading.Thread(
        target=lambda: results.__setitem__("flip", handler.flip_to_reducer())
    )
    flipper.start()
    for _ in range(1000):
        if fence.closed:
            break
        threading.Event().wait(0.005)
    assert fence.closed, "the handover never closed admission"

    # A new command during the handover is answered retry and appends nothing anywhere.
    during = _append(handler, _request(new_id, "during"))
    assert during.status is EnumWorkLedgerAppendStatus.RETRY
    assert during.exit_code == 75
    assert judge.calls == 1
    assert authority.rows() == []
    assert flipper.is_alive(), (
        "the handover finished while an append was still in flight"
    )

    judge.release.set()
    writer.join(timeout=10)
    flipper.join(timeout=10)
    assert not writer.is_alive()
    assert not flipper.is_alive()

    landed = results["in_flight"]
    assert landed.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert handler.authority is EnumLedgerAuthority.REDUCER
    assert results["flip"].fence_seq == FILE_SEQ_BASE + 1
    assert _file_row_count(ledger) == 1

    # The writers retry under the same request ids: the new command lands once on the reducer, and the
    # in-flight one is answered duplicate from the reducer's records, not appended a second time.
    retried = _append(handler, _request(new_id, "during"))
    assert retried.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert retried.ledger_seqs == [FILE_SEQ_BASE + 2]
    again = _append(handler, _request(in_flight_id, "in flight"))
    assert again.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert again.ledger_seqs == [FILE_SEQ_BASE + 1]
    twice = _append(handler, _request(new_id, "during"))
    assert twice.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert _file_row_count(ledger) == 1
    assert (
        len(authority.rows()) == 1
    )  # the file judge's row is adopted by sequence, the new row is held
    assert authority.highest_seq() == FILE_SEQ_BASE + 2
    assert judge.calls == 1


def test_fenced_handover_in_flight_drain_timeout_leaves_authority_and_lifts_the_fence(
    tmp_path: Path,
) -> None:
    """Error chain: an append that never finishes fails the handover; authority stays and writers go on."""
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    handler, judge = _handler(
        ledger,
        InMemoryLedgerAuthority(),
        InMemoryJudgeAcceptanceStore(),
        drain_wait_seconds=0.05,
    )
    fence = handler.flip_fence
    assert fence is not None
    judge.release.clear()
    writer = threading.Thread(target=lambda: _append(handler, _request(uuid4())))
    writer.start()
    assert judge.started.wait(timeout=10)
    try:
        with pytest.raises(FenceDrainTimeoutError):
            handler.flip_to_reducer()
    finally:
        judge.release.set()
        writer.join(timeout=10)
    assert handler.authority is EnumLedgerAuthority.FILE
    assert fence.closed is False
    assert (
        _append(handler, _request(uuid4())).status
        is EnumWorkLedgerAppendStatus.ACCEPTED
    )


def test_rollback_without_file_recreate_restarts_the_file_judge_from_the_reducer(
    tmp_path: Path,
) -> None:
    """AC3: the rollback restarts the old judge from the reducer's records and never recreates the file."""
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    store = InMemoryJudgeAcceptanceStore()
    authority = InMemoryLedgerAuthority()
    restart = _Restart()
    handler, judge = _handler(ledger, authority, store, restart)
    handler.flip_to_reducer()
    committed = uuid4()
    first = handler.handle(
        _request(committed, "after the flip"), writer_principal="developer"
    )
    assert first.ledger_seqs == [1]

    ledger.unlink()  # the file is gone: a rollback that regenerates it would bring it back
    result = handler.rollback_to_file()

    assert result.authority is EnumLedgerAuthority.FILE
    assert result.fence_seq == 1
    assert handler.authority is EnumLedgerAuthority.FILE
    assert not ledger.exists(), "the rollback recreated the file"
    assert judge.calls == 0
    (state,) = restart.states
    assert state.fence_seq == 1
    assert state.decided_request_ids == (str(committed),)

    # A retried committed request is answered duplicate from the reducer's records, with the file absent.
    retried = handler.handle(
        _request(committed, "after the flip"), writer_principal="developer"
    )
    assert retried.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert retried.ledger_seqs == [1]
    assert not ledger.exists()
    assert judge.calls == 0

    # A rolled-back second rollback or flip is idempotent in its own direction.
    assert handler.rollback_to_file().authority is EnumLedgerAuthority.FILE


def test_rollback_without_file_recreate_failed_restart_keeps_the_reducer_authoritative(
    tmp_path: Path,
) -> None:
    """Error chain: the file judge does not start, so authority stays with the reducer and the fence lifts."""
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    handler, judge = _handler(
        ledger,
        InMemoryLedgerAuthority(),
        InMemoryJudgeAcceptanceStore(),
        _Restart(fail=True),
    )
    handler.flip_to_reducer()
    with pytest.raises(RuntimeError, match="did not start"):
        handler.rollback_to_file()
    assert handler.authority is EnumLedgerAuthority.REDUCER
    assert handler.flip_fence is not None
    assert handler.flip_fence.closed is False
    receipt = handler.handle(_request(uuid4()), writer_principal="developer")
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.ledger_lines == []
    assert judge.calls == 0
