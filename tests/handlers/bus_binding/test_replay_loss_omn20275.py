# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A queued ledger append is either landed or its loss reaches the writer (OMN-20275).

Measured 2026-11-01: 61 append requests the writers had been told were queued were refused on replay
and left in ``refused/``; a retry minted a new ``req=`` so acknowledgements repeated; and a multi-row
ruling went out one row at a time.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.bus_binding import outbox, recovery, select
from omnimarket.models.operator_capture import ModelCaptureProcessRequest
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
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_process import (
    HandlerCaptureProcess,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerFile,
    ModelAppendCommandResult,
)
from tests.unit.bus_binding.test_bus_binding_fallback import (
    LANE,
    _Switch,
)
from tests.unit.bus_binding.test_bus_binding_fallback import (
    workspace as workspace,
)
from tests.unit.nodes.node_operator_capture_effect.test_operator_capture_effect import (
    NOW as CAPTURE_NOW,
)
from tests.unit.nodes.node_operator_capture_effect.test_operator_capture_effect import (
    _delegate,
    _ingest,
)
from tests.unit.nodes.node_operator_capture_effect.test_operator_capture_effect import (
    ledger as ledger,
)

pytestmark = pytest.mark.unit

WRITER_LANE = "reader-repair-source-1510"
NOW = datetime(2026, 11, 1, 16, 0, tzinfo=UTC)


def _terminal(request_id: uuid.UUID, stamp: str = "2026-11-01T15:47:02Z") -> str:
    return (
        f"{stamp} | TERMINAL | lane={WRITER_LANE} | ticket=OMN-21339 | outcome=partial"
        f" | friction=FRICTION row of this lane | req={request_id} | via=bus | closed"
    )


def _request(
    request_id: uuid.UUID | None = None,
    stamp: str = "2026-11-01T15:47:02Z",
    at: datetime = NOW,
) -> ModelWorkLedgerAppendRequest:
    request_id = request_id or uuid.uuid4()
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=_terminal(request_id, stamp),
        requested_by_lane=WRITER_LANE,
        requesting_host="h051",
        requested_at=at,
    )


def _exceptions(state: Path) -> list[dict]:
    return (
        create_handler_exception_queue()
        .handle(ModelExceptionQueueRequest(state / "exception-queue", "orchestrator"))
        .rows
    )


async def _refuse(
    request: ModelWorkLedgerAppendRequest,
) -> ModelWorkLedgerAppendReceipt:
    return ModelWorkLedgerAppendReceipt(
        request_id=request.request_id,
        status=EnumWorkLedgerAppendStatus.REFUSED,
        exit_code=65,
        message="ledger_lock: friction guard (OMN-18274) found no FRICTION row for the lane",
        ledger_lines=[],
        ledger_host="h200",
        duration_ms=3,
    )


async def test_refused_reported_to_writer(workspace: Path, tmp_path: Path) -> None:
    """A final refusal reaches the writer's lane even when no outage episode is open."""
    switch, state = _Switch(), tmp_path / "state"
    switch.up = True
    queued = outbox.queue_request(state, _request())
    request_id = outbox.request_id_of(queued)
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
        assert not bound.decision.degraded
        assert not recovery.read_record(state, LANE).get("outage_started_at")
        result = await select.replay_outbox(bound, state, _refuse)
        again = await select.replay_outbox(bound, state, _refuse)
    assert result.refused == 1
    assert again.refused == 0
    kept = list((outbox.outbox_dir(state) / outbox.REFUSED_DIRNAME).glob("*.json"))
    assert [p.name for p in kept] == [queued.name]
    cases = [r for r in _exceptions(state) if r["cls"] == recovery.REFUSED_CLASS]
    assert len(cases) == 1
    assert f"lane={WRITER_LANE}" in cases[0]["key"]
    assert str(request_id) in cases[0]["key"]
    for fact in ("friction guard", "exit_code=65", "h200", "TERMINAL"):
        assert fact in cases[0]["evidence"]
    assert cases[0]["decision"] is False


def test_retry_keeps_request_id(tmp_path: Path) -> None:
    """A retry of a row whose first attempt is unresolved reuses the first attempt's request, so the serve answers duplicate."""
    first = _request(stamp="2026-11-01T15:47:02Z", at=NOW)
    retry = _request(stamp="2026-11-01T15:48:40Z", at=NOW + timedelta(seconds=98))
    assert retry.request_id != first.request_id
    assert outbox.first_attempt(tmp_path, first) == first
    reused = outbox.first_attempt(tmp_path, retry)
    assert reused.request_id == first.request_id
    assert reused.rows == first.rows

    ledger_file = tmp_path / "ledger.md"
    ledger_file.write_text("# Rolling ledger\n", encoding="utf-8")

    class Runner:
        def append(self, rows: str) -> ModelAppendCommandResult:
            with ledger_file.open("a", encoding="utf-8") as stream:
                stream.write(rows + "\n")
            return ModelAppendCommandResult(exit_code=0)

    host = HandlerWorkLedgerAppendEffect(
        Runner(),
        LocalLedgerFile(ledger_file),
        "ledger-host",
        writer_principals=("developer",),
    )
    assert (
        host.handle(first, writer_principal="developer").status
        is EnumWorkLedgerAppendStatus.ACCEPTED
    )
    answered = host.handle(reused, writer_principal="developer")
    assert answered.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert ledger_file.read_text(encoding="utf-8").count("| TERMINAL |") == 1

    # Once the first attempt is answered, the same words are a new row with a new id.
    outbox.settle(tmp_path, first.request_id)
    later = _request(stamp="2026-11-01T16:30:00Z", at=NOW + timedelta(minutes=40))
    assert outbox.first_attempt(tmp_path, later) == later
    # A first attempt that nobody retried within the window does not capture a later row.
    stale = _request(stamp="2026-11-01T18:30:00Z", at=NOW + timedelta(hours=3))
    assert outbox.first_attempt(tmp_path, stale) == stale


async def test_multi_row_one_request(tmp_path: Path, ledger: Path) -> None:
    """The rows one operator prompt yields go to the ledger as one batched append."""
    root = tmp_path / "store"
    assert _ingest(root) is not None
    sent: list[str] = []

    def append(payload: str) -> tuple[bool, str]:
        sent.append(payload)
        return True, "ok"

    result = HandlerCaptureProcess(_delegate(), append, now=CAPTURE_NOW).handle(
        ModelCaptureProcessRequest(store_dir=root, ledger_path=ledger)
    )
    assert (result.processed, result.rows_appended, result.pending) == (1, 2, 0)
    assert len(sent) == 1
    rows = sent[0].splitlines()
    assert len(rows) == 2
    assert "kind=decision" in rows[0]
    assert "kind=ask" in rows[1]
