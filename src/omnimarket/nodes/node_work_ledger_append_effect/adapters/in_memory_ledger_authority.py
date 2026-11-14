# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An in-process reducer for the fenced flip (OMN-23154): one lock, one transaction per command.

It stands in for the database reducer where no database exists (a customer's local ledger and the
node's own tests). The sequence of a row, its content hash and the request's decision are written
under one lock, so no receipt names a sequence that is not committed (``ReceiptTruth``) and the
sequences are gapless.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.models.work_ledger_append import (
    ModelAuthorityDecision,
    ModelWorkLedgerJudgeAcceptance,
)

_ROW_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| ")


def _blocks(rows: str) -> list[str]:
    """One text per ledger row: its first line and the indented continuations that follow."""
    blocks: list[list[str]] = []
    for line in rows.splitlines():
        if _ROW_START.match(line):
            blocks.append([line])
        elif blocks and line.strip():
            blocks[-1].append(line)
    return ["\n".join(block).strip() for block in blocks]


class InMemoryLedgerAuthority:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[tuple[int, str, str]] = []  # (seq, row_id, text)
        self._decisions: dict[str, ModelAuthorityDecision] = {}

    def rows(self) -> list[str]:
        """The texts of the rows this reducer judged, in sequence order.

        A row adopted from the file judge is known by its hash and sequence only, so it is not listed."""
        with self._lock:
            return [text for _, _, text in self._rows if text]

    def submit(
        self, request_id: str, rows: str, *, if_absent_cell: str | None = None
    ) -> ModelAuthorityDecision:
        with self._lock:
            earlier = self._decisions.get(request_id)
            if earlier is not None:
                return ModelAuthorityDecision(
                    verdict="duplicate", ledger_seqs=earlier.ledger_seqs
                )
            if if_absent_cell is not None:
                cell = re.compile(r" \| " + re.escape(if_absent_cell) + r"(?= \||$)")
                present = tuple(
                    seq
                    for seq, _, text in self._rows
                    if text and cell.search(text.splitlines()[0])
                )
                if present:
                    return ModelAuthorityDecision(
                        verdict="already-present", ledger_seqs=present
                    )
            texts = _blocks(rows)
            if not texts:
                return ModelAuthorityDecision(verdict="refused", reason="no ledger row")
            first = self._rows[-1][0] + 1 if self._rows else 1
            seqs = tuple(range(first, first + len(texts)))
            for seq, text in zip(seqs, texts, strict=True):
                self._rows.append((seq, work_ledger_row_id(text), text))
            decision = ModelAuthorityDecision(verdict="accepted", ledger_seqs=seqs)
            self._decisions[request_id] = decision
            return decision

    def adopt(self, acceptances: Sequence[ModelWorkLedgerJudgeAcceptance]) -> None:
        with self._lock:
            known = {row_id for _, row_id, _ in self._rows}
            by_request: dict[str, list[int]] = {}
            for acceptance in sorted(acceptances, key=lambda item: item.ledger_seq):
                if acceptance.row_id in known:
                    continue
                known.add(acceptance.row_id)
                self._rows.append((acceptance.ledger_seq, acceptance.row_id, ""))
                by_request.setdefault(str(acceptance.request_id), []).append(
                    acceptance.ledger_seq
                )
            self._rows.sort(key=lambda row: row[0])
            for request_id, seqs in by_request.items():
                self._decisions.setdefault(
                    request_id,
                    ModelAuthorityDecision(verdict="accepted", ledger_seqs=tuple(seqs)),
                )

    def decision_for(self, request_id: str) -> ModelAuthorityDecision | None:
        with self._lock:
            return self._decisions.get(request_id)

    def highest_seq(self) -> int:
        with self._lock:
            return self._rows[-1][0] if self._rows else 0

    def row_id_at(self, seq: int) -> str | None:
        with self._lock:
            return next((row_id for s, row_id, _ in self._rows if s == seq), None)

    def decided_request_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._decisions)


__all__: list[str] = ["InMemoryLedgerAuthority"]
