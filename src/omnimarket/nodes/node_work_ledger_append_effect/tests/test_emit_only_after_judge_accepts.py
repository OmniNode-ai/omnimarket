# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-23152: a row reaches the bus only after the judge accepted it.

Golden chain: the judge (append effect) accepts a row and records its sequence; the emit node then
spools the typed event carrying that sequence. Error chain: a row the judge never saw, a row stating
a sequence the judge did not give, and a row that states none are each refused at the emit with
nothing spooled. The named injected break: the judge refuses the append, so no acceptance exists and
the emit refuses the row it would have published.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.handlers.work_ledger_judge_acceptance import (
    InMemoryJudgeAcceptanceStore,
)
from omnimarket.models.work_ledger_append.model_work_ledger_judge_acceptance import (
    ModelWorkLedgerJudgeGate,
)
from omnimarket.nodes.node_event_emit_effect import ModelEmitRequest, ModelEmitResult
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerFile,
    ModelAppendCommandResult,
)
from omnimarket.nodes.node_work_ledger_emit_effect import (
    HandlerWorkLedgerEmit,
    ModelWorkLedgerEmitRequest,
    load_judge_gate,
)

pytestmark = pytest.mark.unit

JUDGE_SEQ = 190464


class _Spool:
    def __init__(self) -> None:
        self.requests: list[ModelEmitRequest] = []

    def handle(self, request: ModelEmitRequest) -> ModelEmitResult:
        self.requests.append(request)
        return ModelEmitResult(event_id=request.event_id, published=True)


class _JudgeRunner:
    """The judge's append: lands the rows, or exits 65 when told to refuse."""

    def __init__(self, path: Path, exit_code: int = 0) -> None:
        self.path = path
        self.exit_code = exit_code

    def append(self, rows: str) -> ModelAppendCommandResult:
        if self.exit_code == 0:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(rows + "\n")
        return ModelAppendCommandResult(exit_code=self.exit_code, stderr="")


class _FixedSequence:
    def sequence_of(self, row_id: str) -> int | None:
        return JUDGE_SEQ


def _judge(
    path: Path, store: InMemoryJudgeAcceptanceStore, exit_code: int = 0
) -> HandlerWorkLedgerAppendEffect:
    return HandlerWorkLedgerAppendEffect(
        _JudgeRunner(path, exit_code),
        LocalLedgerFile(path),
        "ledger",
        writer_principals=("developer",),
        operator_principal="operator",
        acceptance_recorder=store,
        sequence_source=_FixedSequence(),
    )


def _append_request(request_id: UUID) -> ModelWorkLedgerAppendRequest:
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-11-08T12:00:00Z | STATUS | lane=test | ticket=OMN-1 | req={request_id} | exact text",
        requested_by_lane="lab",
        requesting_host="lab-host",
        requested_at=datetime.now(UTC),
    )


def _accepted_row(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _emit(
    row: str, store: InMemoryJudgeAcceptanceStore, spool: _Spool, seq: int | None
) -> Any:
    return HandlerWorkLedgerEmit(emitter=spool, acceptance_reader=store).handle(
        ModelWorkLedgerEmitRequest(row=row, ledger_seq=seq)
    )


def test_emit_only_after_judge_accepts_golden(tmp_path: Path) -> None:
    """The judge accepts, then the emit spools the event with the judge's sequence."""
    store = InMemoryJudgeAcceptanceStore()
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    receipt = _judge(ledger, store).handle(
        _append_request(uuid4()), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    row = _accepted_row(ledger)
    held = store.acceptance_for(work_ledger_row_id(row))
    assert held is not None
    assert held.ledger_seq == JUDGE_SEQ

    spool = _Spool()
    result = _emit(row, store, spool, JUDGE_SEQ)
    assert result.accepted is True
    assert len(spool.requests) == 1
    payload = spool.requests[0].payload
    assert isinstance(payload, dict)
    assert payload["ledger_seq"] == JUDGE_SEQ


def test_emit_only_after_judge_accepts_refuses_unjudged_row(tmp_path: Path) -> None:
    """Error chain: a row the judge never saw is refused at the emit and nothing is spooled."""
    store = InMemoryJudgeAcceptanceStore()
    spool = _Spool()
    row = "2026-11-08T12:00:00Z | FRICTION | lane=hook | ticket=OMN-1 | never judged"
    for seq in (None, JUDGE_SEQ):
        result = _emit(row, store, spool, seq)
        assert result.accepted is False
        assert "unjudged-row-refused" in (result.refusal or "")
    assert spool.requests == []


def test_emit_only_after_judge_accepts_refuses_a_borrowed_sequence(
    tmp_path: Path,
) -> None:
    """A row that states a sequence other than the one the judge gave is refused."""
    store = InMemoryJudgeAcceptanceStore()
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    _judge(ledger, store).handle(_append_request(uuid4()), writer_principal="developer")
    spool = _Spool()
    result = _emit(_accepted_row(ledger), store, spool, JUDGE_SEQ + 1)
    assert result.accepted is False
    assert str(JUDGE_SEQ) in (result.refusal or "")
    assert spool.requests == []


def test_emit_only_after_judge_accepts_injected_break_judge_refuses(
    tmp_path: Path,
) -> None:
    """Named break: the judge's append fails (exit 65), so nothing was accepted and the emit refuses."""
    store = InMemoryJudgeAcceptanceStore()
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    request = _append_request(uuid4())
    receipt = _judge(ledger, store, exit_code=65).handle(
        request, writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    spool = _Spool()
    result = _emit(request.rows, store, spool, JUDGE_SEQ)
    assert result.accepted is False
    assert spool.requests == []


def test_emit_gate_is_declared_in_the_contract_and_required() -> None:
    """The gate's setting lives in the contract; a contract that required nothing would let all through."""
    gate = load_judge_gate()
    assert gate.require_acceptance is True
    open_gate = ModelWorkLedgerJudgeGate(require_acceptance=False, refusal_prefix="x")
    spool = _Spool()
    result = HandlerWorkLedgerEmit(emitter=spool, judge_gate=open_gate).handle(
        ModelWorkLedgerEmitRequest(
            row="2026-11-08T12:00:00Z | FRICTION | lane=hook | ticket=OMN-1 | open"
        )
    )
    assert result.accepted is True
