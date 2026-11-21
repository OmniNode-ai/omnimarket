# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One review lock per (repo#pr, head), recorded on the ledger by the review step.

``acquire`` reads the repository's STATUS rows, refuses when the key already holds a verdict row or
a lock row (the first one's pointer is returned), and otherwise writes one lock row through the
ledger writer and confirms it is the first lock row for the key, so two acquires racing for one head
let only the first row in ledger order through. A ledger that cannot be read or written is
UNDECIDED, never ACQUIRED: a dispatcher that cannot lock does not dispatch.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_review_lock_effect.handlers.ledger_rows import (
    is_lock_row,
    is_verdict_row,
    row_head,
    row_pr,
    verdict_tag,
)
from omnimarket.nodes.node_review_lock_effect.models.model_review_lock import (
    EnumLockOutcome,
    ModelLockPointer,
    ModelReviewLockRequest,
    ModelReviewLockResult,
    ModelStatusRow,
)
from omnimarket.nodes.node_review_lock_effect.protocols import (
    ProtocolLedgerRowWriter,
    ProtocolLedgerStatusReader,
)

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(UTC)


class HandlerReviewLock:
    """Acquire the review lock of one pull request head."""

    def __init__(
        self,
        reader: ProtocolLedgerStatusReader | None = None,
        writer: ProtocolLedgerRowWriter | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._clock = clock

    def handle(self, request: ModelReviewLockRequest) -> ModelReviewLockResult:
        # The runtime builds this handler from its injectable parameters alone, so the ledger
        # ports default to None; a handler with no ledger wired cannot lock, so it is UNDECIDED.
        reader, writer = self._reader, self._writer
        if reader is None or writer is None:
            return self._undecided(request, "ledger reader and writer not wired")
        try:
            rows = reader.status_rows(request.repo)
        except OSError as exc:
            return self._undecided(request, f"ledger unreadable: {exc}")
        refusal = self._refusal(request, rows)
        if refusal is not None:
            return refusal
        if request.dry_run:
            return ModelReviewLockResult(
                outcome=EnumLockOutcome.ACQUIRED,
                key=request.key,
                lock_lane=request.lock_lane,
                detail="dry run: no row written",
            )
        row = self._lock_row(request)
        try:
            writer.append(row)
        except OSError as exc:
            return self._undecided(request, f"lock row not written: {exc}")
        return self._confirm(reader, request, row)

    def _refusal(
        self,
        request: ModelReviewLockRequest,
        rows: Sequence[ModelStatusRow],
    ) -> ModelReviewLockResult | None:
        verdicts = [r for r in rows if self._matches(r, request) and is_verdict_row(r)]
        if verdicts:
            return self._refused(request, verdicts[0], EnumLockOutcome.REFUSED_VERDICT)
        locks = [r for r in rows if self._matches(r, request) and is_lock_row(r)]
        live = [r for r in locks if not self._expired(r, request)]
        if live:
            return self._refused(request, live[0], EnumLockOutcome.REFUSED_LOCK)
        return None

    def _confirm(
        self,
        reader: ProtocolLedgerStatusReader,
        request: ModelReviewLockRequest,
        row: str,
    ) -> ModelReviewLockResult:
        try:
            rows = reader.status_rows(request.repo)
        except OSError as exc:
            return self._undecided(
                request, f"lock row written, ledger unreadable after: {exc}"
            )
        locks = [
            r
            for r in rows
            if self._matches(r, request)
            and is_lock_row(r)
            and not self._expired(r, request)
        ]
        ours = [r for r in locks if r.lane == request.lock_lane]
        if ours and locks[0] != ours[0]:
            return self._refused(
                request,
                locks[0],
                EnumLockOutcome.REFUSED_LOCK,
                "lost the race: an earlier lock row exists",
            )
        detail = (
            ""
            if ours
            else "lock row accepted by the writer, not yet visible in the ledger read"
        )
        return ModelReviewLockResult(
            outcome=EnumLockOutcome.ACQUIRED,
            key=request.key,
            lock_lane=request.lock_lane,
            row=row,
            detail=detail,
        )

    def _matches(self, row: ModelStatusRow, request: ModelReviewLockRequest) -> bool:
        return (
            row_pr(row) == (request.repo, request.pr)
            and row_head(row) == request.head12
        )

    def _expired(self, lock: ModelStatusRow, request: ModelReviewLockRequest) -> bool:
        if request.lock_ttl_minutes is None:
            return False
        try:
            written = datetime.strptime(lock.ts, _TS_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            return False
        return self._clock() - written > timedelta(minutes=request.lock_ttl_minutes)

    def _lock_row(self, request: ModelReviewLockRequest) -> str:
        stamp = self._clock().strftime(_TS_FORMAT)
        return (
            f"{stamp} | STATUS | lane={request.lock_lane} | actor=review-lock | model=none"
            f" | state=review-lock | repo={request.repo} | pr={request.repo}#{request.pr}"
            f" | head={request.head12}"
            f" | Review lock for {request.repo}#{request.pr} at head {request.head12}, taken by dispatch {request.tag}"
        )

    def _refused(
        self,
        request: ModelReviewLockRequest,
        first: ModelStatusRow,
        outcome: EnumLockOutcome,
        note: str = "",
    ) -> ModelReviewLockResult:
        pointer = ModelLockPointer(
            kind="verdict" if outcome is EnumLockOutcome.REFUSED_VERDICT else "lock",
            pointer=self._pointer(request, first),
            ledger_row_id=first.row_id,
            lane=first.lane,
            verdict=first.cells.get("verdict"),
        )
        what = (
            "a verdict"
            if outcome is EnumLockOutcome.REFUSED_VERDICT
            else "a review lock"
        )
        detail = f"{request.key} already has {what}: {pointer.pointer}"
        return ModelReviewLockResult(
            outcome=outcome,
            key=request.key,
            lock_lane=request.lock_lane,
            pointer=pointer,
            detail=f"{note}; {detail}" if note else detail,
        )

    def _pointer(self, request: ModelReviewLockRequest, first: ModelStatusRow) -> str:
        if is_verdict_row(first) and request.verdict_dir:
            name = f"verdict-{request.repo}-{request.pr}-{verdict_tag(first)}.md"
            path = Path(request.verdict_dir) / name
            if path.is_file():
                return str(path)
        return f"ledger:{first.row_id}"

    def _undecided(
        self, request: ModelReviewLockRequest, why: str
    ) -> ModelReviewLockResult:
        return ModelReviewLockResult(
            outcome=EnumLockOutcome.UNDECIDED,
            key=request.key,
            lock_lane=request.lock_lane,
            detail=why,
        )
