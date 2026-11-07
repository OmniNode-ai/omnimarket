# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rebuild the work-ledger table from the accepted-row topic and compare it with the live one (OMN-22915, L12).

Pure: events in, a table and its comparison out. Order is the topic's ordering contract, never a
clock: bootstrap history by ``legacy_ordinal``, then rows by ``ledger_seq``. A row seen twice adds
nothing; one sequence claimed by two rows, one row under two sequences, or a ``row_id`` that is not the
hash of its text is refused, because a rebuild that guessed would equal the live table by luck.
The comparison is row for row and sequence for sequence over the log columns, and entity for entity
over the state columns; ``source`` is left out because a bootstrap row and its live twin differ in it
by design.
"""

from __future__ import annotations

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.handlers.work_ledger_fold import (
    WorkLedgerFoldError,
    apply_ops,
    fold_row,
)
from omnimarket.models.model_work_ledger_fold_request import ModelWorkLedgerFoldRequest
from omnimarket.models.model_work_ledger_fold_result import ModelWorkLedgerRowRecord
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_rebuild import (
    ModelAcceptedRowEvent,
    ModelRebuildRequest,
    ModelRebuildResult,
    ModelRebuiltEntity,
    ModelTableDifference,
    ModelWorkLedgerTable,
)


class WorkLedgerRebuildError(ValueError):
    """The accepted-row events cannot be rebuilt into one table."""


def _order_key(event: ModelAcceptedRowEvent) -> tuple[int, int]:
    if event.legacy_ordinal is not None:
        return (0, event.legacy_ordinal)
    if event.ledger_seq is not None:
        return (1, event.ledger_seq)
    raise WorkLedgerRebuildError(
        f"row {event.row_id} carries neither legacy_ordinal nor ledger_seq: its place is unknown"
    )


def _row_columns(row: ModelWorkLedgerRowRecord) -> dict[str, object]:
    return {
        "ledger_id": row.ledger_id,
        "row_ts": row.row_ts,
        "row_type": row.row_type,
        "row_lane": row.row_lane,
        "tickets": row.tickets,
        "raw_row": row.raw_row,
        "ledger_seq": row.ledger_seq,
    }


class HandlerWorkLedgerRebuild:
    def handle(self, request: ModelRebuildRequest) -> ModelRebuildResult:
        rebuilt = self.rebuild(request.events)
        if request.live is None:
            return ModelRebuildResult(rebuilt=rebuilt, compared=False, equal=False)
        differences = self.compare(rebuilt, request.live)
        return ModelRebuildResult(
            rebuilt=rebuilt,
            compared=True,
            equal=not differences,
            differences=differences,
        )

    def rebuild(
        self, events: tuple[ModelAcceptedRowEvent, ...]
    ) -> ModelWorkLedgerTable:
        rows: dict[str, ModelWorkLedgerRowRecord] = {}
        seq_owner: dict[int, str] = {}
        state: dict[str, dict[str, object]] = {}
        for event in sorted(events, key=_order_key):
            if event.row_id != work_ledger_row_id(event.raw_row):
                raise WorkLedgerRebuildError(
                    f"row_id {event.row_id} is not the hash of its raw_row"
                )
            if event.ledger_seq is not None:
                owner = seq_owner.setdefault(event.ledger_seq, event.row_id)
                if owner != event.row_id:
                    raise WorkLedgerRebuildError(
                        f"ledger_seq {event.ledger_seq} is claimed by rows {owner} and {event.row_id}"
                    )
            seen = rows.get(event.row_id)
            if seen is not None:
                if (
                    seen.ledger_seq != event.ledger_seq
                    and event.ledger_seq is not None
                    and seen.ledger_seq is not None
                ):
                    raise WorkLedgerRebuildError(
                        f"row {event.row_id} is accepted under two sequences "
                        f"({seen.ledger_seq} and {event.ledger_seq})"
                    )
                continue  # a replayed row adds nothing
            try:
                folded = fold_row(
                    ModelWorkLedgerFoldRequest(
                        raw_row=event.raw_row,
                        ledger_id=event.ledger_id,
                        source=event.source.value,
                        ledger_seq=event.ledger_seq,
                    )
                )
            except WorkLedgerFoldError as exc:
                raise WorkLedgerRebuildError(
                    f"accepted row {event.row_id} does not fold: {exc}"
                ) from exc
            rows[event.row_id] = folded.row
            apply_ops(state, folded.ops)
        return ModelWorkLedgerTable(
            rows=tuple(
                sorted(
                    rows.values(),
                    key=lambda r: (r.ledger_seq is None, r.ledger_seq or 0, r.row_id),
                )
            ),
            state=tuple(
                ModelRebuiltEntity(entity_key=key, **values)
                for key, values in sorted(state.items())
            ),
        )

    def compare(
        self, rebuilt: ModelWorkLedgerTable, live: ModelWorkLedgerTable
    ) -> tuple[ModelTableDifference, ...]:
        found: list[ModelTableDifference] = []
        rebuilt_rows = {r.row_id: r for r in rebuilt.rows}
        live_rows = {r.row_id: r for r in live.rows}
        for row_id in sorted(live_rows.keys() - rebuilt_rows.keys()):
            found.append(
                ModelTableDifference(
                    key=row_id,
                    where="rows",
                    difference="in the live table, absent from the rebuild",
                )
            )
        for row_id in sorted(rebuilt_rows.keys() - live_rows.keys()):
            found.append(
                ModelTableDifference(
                    key=row_id,
                    where="rows",
                    difference="in the rebuild, absent from the live table",
                )
            )
        for row_id in sorted(rebuilt_rows.keys() & live_rows.keys()):
            mine, theirs = (
                _row_columns(rebuilt_rows[row_id]),
                _row_columns(live_rows[row_id]),
            )
            for column in sorted(k for k in mine if mine[k] != theirs[k]):
                found.append(
                    ModelTableDifference(
                        key=row_id,
                        where="rows",
                        difference=f"{column}: rebuild {mine[column]!r}, live {theirs[column]!r}",
                    )
                )
        rebuilt_state = {e.entity_key: e for e in rebuilt.state}
        live_state = {e.entity_key: e for e in live.state}
        for key in sorted(live_state.keys() - rebuilt_state.keys()):
            found.append(
                ModelTableDifference(
                    key=key,
                    where="state",
                    difference="in the live table, absent from the rebuild",
                )
            )
        for key in sorted(rebuilt_state.keys() - live_state.keys()):
            found.append(
                ModelTableDifference(
                    key=key,
                    where="state",
                    difference="in the rebuild, absent from the live table",
                )
            )
        for key in sorted(rebuilt_state.keys() & live_state.keys()):
            mine_state, live_dump = (
                rebuilt_state[key].model_dump(),
                live_state[key].model_dump(),
            )
            for column in sorted(
                k for k in mine_state if mine_state[k] != live_dump[k]
            ):
                found.append(
                    ModelTableDifference(
                        key=key,
                        where="state",
                        difference=f"{column}: rebuild {mine_state[column]!r}, live {live_dump[column]!r}",
                    )
                )
        return tuple(found)
