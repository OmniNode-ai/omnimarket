# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Queue rows appended in file mode and replay them once the bus accepts (OMN-21849).

A lane that appends in file mode (the bus refused the write) leaves a row in the file
that never reaches the projection. ``enqueue`` records such a row by its id; ``drain``
hands every pending row, oldest first, to the injected publisher (the work-ledger emit
path), and marks a row replayed only when the publisher reports it published. A bus that
refuses leaves the row pending with the reason and the attempt count; a row that can
never be typed is marked refused with its reason, so the seam counter counts it with
that reason. Replay is idempotent by row id: the id is the delivery identity downstream.

``replay_from_file`` (OMN-19998) heals a row that was appended and never reached the
projection (the dual write's emit failed open and left only a failure-log line). It reads
the named rows, or a time window of rows, from the ledger file of record, queues them by
row id in the same durable queue (so each is a retry record with its attempts and last
reason until it is published) and drains exactly those rows. A row the queue already shows
replayed is reported and not sent again; a requested id the file does not hold is reported
and nothing is sent for it.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.ledger_file_rows import (
    ProtocolLedgerFileRows,
    select_rows,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.replay_queue import (
    ProtocolReplayQueue,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.models.model_file_mode_replay import (
    EnumReplayOperation,
    ModelFileModeReplayRequest,
    ModelFileModeReplayResult,
    ModelReplayPending,
    ModelReplayPublishOutcome,
    ModelReplayRefused,
)


class ProtocolRowPublisher(Protocol):
    """Publishes one exact ledger row through the work-ledger emit path."""

    def publish_row(self, row: str, ledger_id: str) -> ModelReplayPublishOutcome: ...


_SPOOL_ONLY = "accepted on the spool, not yet published"


class HandlerFileModeReplay:
    def __init__(
        self,
        *,
        queue: ProtocolReplayQueue,
        publisher: ProtocolRowPublisher,
        file_rows: ProtocolLedgerFileRows | None = None,
    ) -> None:
        self._queue = queue
        self._publisher = publisher
        self._file_rows = file_rows

    def handle(self, request: ModelFileModeReplayRequest) -> ModelFileModeReplayResult:
        if request.operation is EnumReplayOperation.ENQUEUE:
            return self._enqueue(request)
        if request.operation is EnumReplayOperation.REPLAY_FROM_FILE:
            return self._replay_from_file(request)
        return self._drain(request)

    def _replay_from_file(
        self, request: ModelFileModeReplayRequest
    ) -> ModelFileModeReplayResult:
        if self._file_rows is None:
            raise ValueError("replay_from_file needs the ledger file rows injected")
        chosen, missing = select_rows(
            self._file_rows,
            row_ids=request.row_ids,
            since=request.window_since,
            until=request.window_until,
        )
        already_replayed = self._queue.replayed_row_ids()
        wanted: set[str] = set()
        skipped: list[str] = []
        for row in chosen:
            row_id = work_ledger_row_id(row)
            if row_id in already_replayed:
                skipped.append(row_id)
                continue
            self._queue.enqueue(row_id, row)
            wanted.add(row_id)
        drained = self._drain(request, only=wanted)
        return drained.model_copy(
            update={
                "operation": request.operation,
                "selected": len(chosen),
                "already_replayed": skipped,
                "not_in_file": missing,
            }
        )

    def _enqueue(
        self, request: ModelFileModeReplayRequest
    ) -> ModelFileModeReplayResult:
        enqueued = already = 0
        for row in request.rows:
            if self._queue.enqueue(work_ledger_row_id(row), row.strip()):
                enqueued += 1
            else:
                already += 1
        return ModelFileModeReplayResult(
            operation=request.operation, enqueued=enqueued, already_queued=already
        )

    def _drain(
        self,
        request: ModelFileModeReplayRequest,
        *,
        only: set[str] | None = None,
    ) -> ModelFileModeReplayResult:
        replayed: list[str] = []
        still_pending: list[ModelReplayPending] = []
        refused: list[ModelReplayRefused] = []
        for queued in list(self._queue.pending()):
            if only is not None and queued.row_id not in only:
                continue
            try:
                outcome = self._publisher.publish_row(queued.row, request.ledger_id)
            except Exception as exc:  # effect boundary: the bus path failing is a retry
                outcome = ModelReplayPublishOutcome(
                    accepted=False, published=False, error=type(exc).__name__
                )
            if outcome.refusal:
                self._queue.mark_refused(queued.row_id, outcome.refusal)
                refused.append(
                    ModelReplayRefused(row_id=queued.row_id, reason=outcome.refusal)
                )
            elif outcome.accepted and outcome.published:
                self._queue.mark_replayed(queued.row_id)
                replayed.append(queued.row_id)
            else:
                reason = outcome.error or _SPOOL_ONLY
                attempts = self._queue.record_attempt(queued.row_id, reason)
                still_pending.append(
                    ModelReplayPending(
                        row_id=queued.row_id, attempts=attempts, last_reason=reason
                    )
                )
        return ModelFileModeReplayResult(
            operation=request.operation,
            replayed=replayed,
            still_pending=still_pending,
            refused=refused,
        )


__all__ = ["HandlerFileModeReplay", "ProtocolRowPublisher"]
