# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publishes one ledger row from the ledger file through the work-ledger emit node (OMN-19998).

The row was read from the ledger file of record, so the judge that owns that file already
accepted it: the file is the acceptance evidence, and the live emit command's judge gate
(a ledger_seq the judge assigned) does not apply to a replay. The row is typed by the emit
node's own parser and handed to its emit path (``HandlerWorkLedgerEmit.emit_event``) under
the replay source the overlay names. The event id is the row id's UUID5, so a second replay
of the same row names the same spool record and the projection folds it once.
"""

from __future__ import annotations

from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    LedgerRowRefusalError,
    parse_ledger_row,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.models.model_file_mode_replay import (
    ModelReplayPublishOutcome,
)


class EmitPathPublisher:
    """ProtocolRowPublisher over the emit node's emit path, for rows read from the ledger file."""

    def __init__(self, *, emit: HandlerWorkLedgerEmit, source: str) -> None:
        self._emit = emit
        self._source = source

    def publish_row(self, row: str, ledger_id: str) -> ModelReplayPublishOutcome:
        try:
            event = parse_ledger_row(row, ledger_id=ledger_id, source=self._source)
        except LedgerRowRefusalError as exc:
            return ModelReplayPublishOutcome(
                accepted=False, published=False, refusal=str(exc)
            )
        try:
            emitted = self._emit.emit_event(event)
        except Exception as exc:  # effect boundary: a spool or guard refusal is a retry
            return ModelReplayPublishOutcome(
                accepted=False, published=False, error=repr(exc)
            )
        return ModelReplayPublishOutcome(accepted=True, published=emitted.published)


__all__ = ["EmitPathPublisher"]
