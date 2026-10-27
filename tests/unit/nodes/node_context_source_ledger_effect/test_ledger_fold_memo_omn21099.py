# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared work-ledger folds preserve all three context tiers (OMN-21099)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)

from omnimarket.handlers.work_ledger_fold import (
    WorkLedgerFoldMemo,
    fold_row,
    parse_stamp,
)
from omnimarket.models.model_work_ledger_fold_request import ModelWorkLedgerFoldRequest
from omnimarket.models.model_work_ledger_fold_result import ModelWorkLedgerFoldResult
from omnimarket.nodes.node_context_source_ledger_effect import (
    HandlerContextSourceLedger,
)
from omnimarket.nodes.node_context_source_ledger_effect.handlers.ledger_row_reader import (
    MarkdownLedgerRowReader,
    ModelLedgerSourceRow,
    ProtocolLedgerRowReader,
)
from omnimarket.nodes.node_context_source_work_state_effect import (
    HandlerContextSourceWorkState,
)
from omnimarket.nodes.node_context_source_work_state_effect.handlers.handler_context_source_work_state import (
    ModelWorkStateBinding,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import row_id_of

TENANT = "lab"
WATERMARK = "2026-10-19T08:00:00Z"
RULING = '2026-10-19T06:00:00Z | RULING | lane=orch-a | ticket=OMN-1 | kind=decision | "use the lab" | first answer'
REPLACEMENT = '2026-10-19T06:05:00Z | RULING | lane=orch-a | ticket=OMN-1 | kind=decision | supersedes=2026-10-19T06:00:00Z | "use the pool" | replaces it'
CORRECTION = "2026-10-19T06:10:00Z | CORRECTION | lane=orch-a | ticket=OMN-1 | corrects=2026-10-19T06:05:00Z | operator_span=0:12 | the operator's words"
CONSENT = '2026-10-19T06:15:00Z | OPERATOR-CONSENT | lane=orch-a | "yes" | APPROVED SCOPE: lab | OUT OF SCOPE: prod | evidence'
HANDOFF = "2026-10-19T07:00:00Z | MSG | from=orch-a | id=2026-10-19T07:00:00Z-orch-a | to=orch-a | ticket=OMN-1 | finish the read path"
ANSWERED = "2026-10-19T07:05:00Z | MSG | from=orch-a | id=2026-10-19T07:05:00Z-orch-a | to=orch-a | ticket=OMN-1 | already answered"
ACK = "2026-10-19T07:06:00Z | ACK | lane=orch-a | re=2026-10-19T07:05:00Z-orch-a | ticket=OMN-1 | done"
OPEN_CHILD = "2026-10-19T07:20:00Z | CLAIM | lane=child-1 | actor=codex | ticket=OMN-2 | parent=sess-p | worktree=none | est ~1 lane-hours; displaces x; (OMN-2) | open child"
CLOSED_CHILD = "2026-10-19T07:21:00Z | CLAIM | lane=child-2 | actor=codex | ticket=OMN-3 | parent=sess-p | worktree=none | est ~1 lane-hours; displaces x; (OMN-3) | closed child"
CHILD_DONE = "2026-10-19T07:30:00Z | TERMINAL | lane=child-2 | outcome=done | friction=none | delegated=0 delegation_reason=no-text-or-code | done"
STALE_HANDOFF = "2026-10-17T07:00:00Z | MSG | from=orch-a | id=2026-10-17T07:00:00Z-orch-a | to=orch-a | ticket=OMN-1 | two days old"
LATE_HANDOFF = "2026-10-19T09:00:00Z | MSG | from=orch-a | id=2026-10-19T09:00:00Z-orch-a | to=orch-a | ticket=OMN-1 | after the watermark"
ROWS = (
    STALE_HANDOFF,
    RULING,
    REPLACEMENT,
    CORRECTION,
    CONSENT,
    HANDOFF,
    ANSWERED,
    ACK,
    OPEN_CHILD,
    CLOSED_CHILD,
    CHILD_DONE,
    LATE_HANDOFF,
)
PAIRS = (("ruling", RULING), ("handoff", HANDOFF))


class _CountingFold:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(
        self, request: ModelWorkLedgerFoldRequest
    ) -> ModelWorkLedgerFoldResult:
        self.calls.append(request.raw_row)
        return fold_row(request)


class _ListReader:
    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]:
        return tuple(
            ModelLedgerSourceRow(
                row_id=row_id_of(raw),
                raw_row=raw,
                source_ref=f"work_ledger_rows:{row_id_of(raw)}",
            )
            for raw in ROWS
            if parse_stamp(raw[:20]) <= as_of
        )


def _request(factor: EnumContextFactor) -> ModelContextSourceRequest:
    return ModelContextSourceRequest(
        factor=factor,
        scopes=("session:sess-p",),
        caller_scope=ModelContextCallerScope(lane="orch-a"),
        access=ModelContextAccess(tenant_id=TENANT),
        revision=WATERMARK,
    )


def _work_state(
    reader: ProtocolLedgerRowReader, memo: WorkLedgerFoldMemo
) -> HandlerContextSourceWorkState:
    return HandlerContextSourceWorkState(
        reader=reader,
        tenant_ids=(TENANT,),
        fold_memo=memo,
        work_state=ModelWorkStateBinding(horizon_hours=24, open_work_senders="self"),
    )


@pytest.mark.unit
def test_unchanged_rows_are_folded_once_across_passes() -> None:
    fold = _CountingFold()
    memo = WorkLedgerFoldMemo(fold)
    first = memo.fold_rows(PAIRS)
    second = memo.fold_rows(PAIRS)
    assert fold.calls == [RULING, HANDOFF]
    assert [result.row.raw_row for result in first if result is not None] == [
        RULING,
        HANDOFF,
    ]
    assert all(a is b for a, b in zip(first, second, strict=True))


@pytest.mark.unit
def test_changed_raw_row_under_the_same_key_is_folded_again() -> None:
    fold = _CountingFold()
    memo = WorkLedgerFoldMemo(fold)
    memo.fold_rows(PAIRS)
    changed = RULING.replace("use the lab", "use the pool")
    results = memo.fold_rows((("ruling", changed), PAIRS[1]))
    assert fold.calls == [RULING, HANDOFF, changed]
    assert results[0] is not None
    assert results[0].row.raw_row == changed


@pytest.mark.unit
@pytest.mark.parametrize("subset", [(PAIRS[0],), ()])
def test_keys_absent_from_the_latest_pass_are_dropped(
    subset: tuple[tuple[str, str], ...],
) -> None:
    fold = _CountingFold()
    memo = WorkLedgerFoldMemo(fold)
    memo.fold_rows(PAIRS)
    memo.fold_rows(PAIRS)
    memo.fold_rows(subset)
    assert fold.calls == [RULING, HANDOFF]
    memo.fold_rows((PAIRS[1],))
    assert fold.calls == [RULING, HANDOFF, HANDOFF]


@pytest.mark.unit
def test_a_fold_refusal_is_memoized_as_none() -> None:
    fold = _CountingFold()
    memo = WorkLedgerFoldMemo(fold)
    rows = (PAIRS[0], ("invalid", "not a canonical ledger row"), PAIRS[1])
    first = memo.fold_rows(rows)
    second = memo.fold_rows(rows)
    assert first[0] is not None
    assert first[1] is None
    assert first[2] is not None
    assert second == first
    assert fold.calls == [RULING, "not a canonical ledger row", HANDOFF]


@pytest.mark.unit
@pytest.mark.parametrize("markdown", [False, True])
def test_three_tiers_share_folds_with_equal_results(
    tmp_path: Path, markdown: bool
) -> None:
    reader: ProtocolLedgerRowReader
    if markdown:
        path = tmp_path / "ROLLING_WORK_LEDGER.md"
        path.write_text("# ledger\n\n" + "\n".join(ROWS) + "\n", encoding="utf-8")
        reader = MarkdownLedgerRowReader([path], manifest_root=tmp_path)
    else:
        reader = _ListReader()
    fold = _CountingFold()
    memo = WorkLedgerFoldMemo(fold)
    ledger = HandlerContextSourceLedger(
        reader=reader, tenant_ids=(TENANT,), fold_memo=memo
    )
    work_state = _work_state(reader, memo)
    factors = (
        EnumContextFactor.OPERATOR_RULINGS,
        EnumContextFactor.OPEN_WORK,
        EnumContextFactor.ORCHESTRATION_STATE,
    )
    expected_texts = ([REPLACEMENT, CONSENT], [HANDOFF], [OPEN_CHILD])
    actual: ModelContextSourceResult
    expected: ModelContextSourceResult
    for factor, texts in zip(factors, expected_texts, strict=True):
        request = _request(factor)
        if factor is EnumContextFactor.OPERATOR_RULINGS:
            actual = ledger.handle(request)
            expected = HandlerContextSourceLedger(
                reader=reader, tenant_ids=(TENANT,), fold_memo=WorkLedgerFoldMemo()
            ).handle(request)
        else:
            actual = work_state.handle(request)
            expected = _work_state(reader, WorkLedgerFoldMemo()).handle(request)
        assert actual.status is EnumContextSourceStatus.RESOLVED
        assert [entry.text for entry in actual.entries] == texts
        # Read and fold durations depend on the clock, not the ledger's contents.
        assert actual.model_dump(exclude={"leg_timings_ms"}) == expected.model_dump(
            exclude={"leg_timings_ms"}
        )
    rows = reader.read_rows(as_of=parse_stamp(WATERMARK))
    assert fold.calls == [row.raw_row for row in rows]
