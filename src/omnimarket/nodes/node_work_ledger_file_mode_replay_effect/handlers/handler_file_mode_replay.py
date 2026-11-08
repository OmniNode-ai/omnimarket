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
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
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
        self, *, queue: ProtocolReplayQueue, publisher: ProtocolRowPublisher
    ) -> None:
        self._queue = queue
        self._publisher = publisher

    def handle(self, request: ModelFileModeReplayRequest) -> ModelFileModeReplayResult:
        if request.operation is EnumReplayOperation.ENQUEUE:
            return self._enqueue(request)
        return self._drain(request)

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

    def _drain(self, request: ModelFileModeReplayRequest) -> ModelFileModeReplayResult:
        replayed: list[str] = []
        still_pending: list[ModelReplayPending] = []
        refused: list[ModelReplayRefused] = []
        for queued in list(self._queue.pending()):
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
