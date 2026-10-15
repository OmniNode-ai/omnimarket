# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the work ledger's row events into one markdown file, in ``ledger_seq`` order.

OMN-20275, unit M6 deliverable 2; the design is the ledger-projection TLA+ model
(``tla/ledger_projection``: model, results and verdict). The rules this handler keeps:

- Identity is ``ledger_seq``; ``row_id`` is compared, never keyed on. A seq present with
  another ``row_id`` blocks the node.
- The rendered file is the watermark ``W`` (baseline plus complete rows). Only ``W+1`` is
  rendered; an event at or below ``W`` is dropped after the ``row_id`` comparison.
- Offsets are committable only over rendered seqs, per topic, as the longest read prefix.
- A gap (a pending event past ``W+1``, or a source head above ``W``) is repaired by reading
  ``W+1`` from the ledger of record. When the source cannot supply it the node blocks,
  reports the seq, and retries on every later step. It never skips a gap.
- The probe reads the head from the ledger of record, so a lost last row is found.
- The pending set and the in-flight offsets are volatile: a restart rebuilds them from
  redelivery and repair.
- A row of more than one line cannot be counted back from the file after a crash, so it
  blocks the node rather than corrupt the watermark.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.events.model_ledger_row_event import DEFAULT_LEDGER_ID
from omnimarket.models.model_work_ledger_render_row import ModelWorkLedgerRenderRow
from omnimarket.nodes.node_work_ledger_render_effect.handlers.rendered_ledger_file import (
    RenderedLedgerFile,
)
from omnimarket.nodes.node_work_ledger_render_effect.models.model_work_ledger_render import (
    EnumWorkLedgerBlockReason,
    EnumWorkLedgerRenderKind,
    ModelWorkLedgerRenderBlock,
    ModelWorkLedgerRenderRequest,
    ModelWorkLedgerRenderResult,
)
from omnimarket.protocols.protocol_work_ledger_record_source import (
    ProtocolWorkLedgerRecordSource,
    WorkLedgerSourceError,
)

# A conflict or a multi-line row is not cleared by a retry: it needs a person.
_STICKY = frozenset(
    {
        EnumWorkLedgerBlockReason.ROW_ID_CONFLICT,
        EnumWorkLedgerBlockReason.MULTI_LINE_ROW,
    }
)


class _Turn:
    """What one step did, collected for its result."""

    def __init__(self) -> None:
        self.rendered: list[int] = []
        self.repaired: list[int] = []
        self.dropped_duplicate = False
        self.head_unavailable = False


class HandlerWorkLedgerRender:
    """One node owns one rendered file (an exclusive lock beside it enforces that)."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        source: ProtocolWorkLedgerRecordSource | None = None,
        ledger_id: str = DEFAULT_LEDGER_ID,
    ) -> None:
        """Bind a rendered file and a ledger-of-record source, or build it unbound.

        An unbound handler (the form the runtime's auto-wiring check constructs) opens
        nothing and refuses to handle; the host process that owns the file binds both.
        """
        if (path is None) != (source is None):
            raise ValueError("bind both the rendered file and the source, or neither")
        self._source = source
        self._ledger_id = ledger_id
        self._file = RenderedLedgerFile(path) if path is not None else None
        if self._file is not None:
            self._file.recover()
        self._pending: dict[int, ModelWorkLedgerRenderRow] = {}
        self._inflight: dict[str, list[tuple[int, int]]] = {}
        self._source_head = 0
        self._blocked: ModelWorkLedgerRenderBlock | None = None

    @property
    def watermark(self) -> int:
        return self._bound_file().watermark

    def _bound_file(self) -> RenderedLedgerFile:
        if self._file is None:
            raise RuntimeError(
                "unbound: construct with the rendered file path and the ledger-of-record source"
            )
        return self._file

    def _bound_source(self) -> ProtocolWorkLedgerRecordSource:
        if self._source is None:
            raise RuntimeError(
                "unbound: construct with the rendered file path and the ledger-of-record source"
            )
        return self._source

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()

    def handle(
        self, request: ModelWorkLedgerRenderRequest
    ) -> ModelWorkLedgerRenderResult:
        turn = _Turn()
        committable = True
        if request.kind is EnumWorkLedgerRenderKind.ROW:
            assert request.row is not None
            self._register(request.topic, request.offset, request.row.ledger_seq)
            committable = self._receive(request.row, turn)
        else:
            self._probe(turn)
        self._advance(turn)
        commit = self._commit_offsets()
        if request.kind is EnumWorkLedgerRenderKind.ROW:
            assert request.row is not None
            committable = committable and request.row.ledger_seq <= self.watermark
        return ModelWorkLedgerRenderResult(
            kind=request.kind,
            ledger_id=self._ledger_id,
            baseline_seq=self._bound_file().baseline_seq,
            watermark=self.watermark,
            rendered_seqs=tuple(turn.rendered),
            repaired_seqs=tuple(turn.repaired),
            dropped_duplicate=turn.dropped_duplicate,
            committable=committable,
            commit_offsets=commit,
            pending_count=len(self._pending),
            source_head=self._source_head if self._source_head else None,
            head_unavailable=turn.head_unavailable,
            blocked=self._blocked,
        )

    def acknowledge(self, topic: str, offset: int) -> dict[str, int]:
        """Mark an event that carries no row (a dead-lettered payload) as handled."""
        self._register(topic, offset, 0)
        return self._commit_offsets()

    def _register(self, topic: str | None, offset: int | None, seq: int) -> None:
        if topic is not None and offset is not None:
            self._inflight.setdefault(topic, []).append((offset, seq))

    def _commit_offsets(self) -> dict[str, int]:
        commit: dict[str, int] = {}
        for topic, entries in self._inflight.items():
            prefix = 0
            while prefix < len(entries) and entries[prefix][1] <= self.watermark:
                prefix += 1
            if prefix:
                commit[topic] = entries[prefix - 1][0]
                del entries[:prefix]
        return commit

    def _receive(self, row: ModelWorkLedgerRenderRow, turn: _Turn) -> bool:
        if row.ledger_id != self._ledger_id:
            raise ValueError(
                f"row names ledger {row.ledger_id!r}, this node renders {self._ledger_id!r}"
            )
        seq = row.ledger_seq
        baseline = self._bound_file().baseline_seq
        if seq <= baseline:
            turn.dropped_duplicate = (
                True  # history before the baseline is never re-rendered
            )
            return True
        if seq <= self.watermark:
            if self._bound_file().row_ids[seq - baseline - 1] == row.row_id:
                turn.dropped_duplicate = True
                return True
            self._block(
                seq, EnumWorkLedgerBlockReason.ROW_ID_CONFLICT, "file row differs"
            )
            return False
        held = self._pending.get(seq)
        if held is None:
            self._pending[seq] = row
            return True
        if held.row_id == row.row_id:
            turn.dropped_duplicate = True
            return True
        self._block(
            seq, EnumWorkLedgerBlockReason.ROW_ID_CONFLICT, "pending row differs"
        )
        return False

    def _probe(self, turn: _Turn) -> None:
        try:
            head = self._bound_source().read_head(ledger_id=self._ledger_id)
        except WorkLedgerSourceError:
            turn.head_unavailable = True
            return
        self._source_head = max(self._source_head, head)

    def _advance(self, turn: _Turn) -> None:
        if self._blocked is not None:
            if self._blocked.reason in _STICKY:
                return
            self._blocked = None  # a gap block is retried on every step
        while True:
            nxt = self.watermark + 1
            row = self._pending.pop(nxt, None)
            repaired = False
            if row is None:
                known = max(self._source_head, max(self._pending, default=0))
                if known <= self.watermark:
                    return
                try:
                    row = self._bound_source().read_row(
                        ledger_id=self._ledger_id, ledger_seq=nxt
                    )
                except WorkLedgerSourceError as exc:
                    self._block(
                        nxt, EnumWorkLedgerBlockReason.SOURCE_UNAVAILABLE, str(exc)
                    )
                    return
                if row is None:
                    self._block(nxt, EnumWorkLedgerBlockReason.SOURCE_MISSING)
                    return
                if row.ledger_seq != nxt or row.ledger_id != self._ledger_id:
                    self._block(
                        nxt,
                        EnumWorkLedgerBlockReason.SOURCE_MISSING,
                        f"source answered seq {row.ledger_seq} for seq {nxt}",
                    )
                    return
                repaired = True
            if "\n" in row.raw_row:
                self._block(nxt, EnumWorkLedgerBlockReason.MULTI_LINE_ROW)
                return
            self._bound_file().append(row.raw_row)
            turn.rendered.append(nxt)
            if repaired:
                turn.repaired.append(nxt)

    def _block(
        self, seq: int, reason: EnumWorkLedgerBlockReason, detail: str = ""
    ) -> None:
        self._blocked = ModelWorkLedgerRenderBlock(
            seq=seq, reason=reason, detail=detail
        )


__all__: list[str] = ["HandlerWorkLedgerRender"]
