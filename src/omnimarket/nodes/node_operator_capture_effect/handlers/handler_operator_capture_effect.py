# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOperatorCaptureEffect: one operator prompt from the bus into ledger rows (OMN-20905).

RULING 2026-10-10T22:40:43Z: the operator's decisions and asks are extracted asynchronously from
the bus, not by a capture hook. The capture producer marks the prompts the operator typed
(``content_kind`` ``operator_prompt``); this handler takes one such record into the local store,
then runs the capture worker over the store's inbox: classification through ``onex delegate``
(deterministic fallback when it cannot answer), drift against earlier rulings, one ledger row per
item through the ledger's own append command. A prompt whose rows are not all appended stays in
the inbox and is retried, never dropped. Every decision is node_operator_capture_compute's.

An assistant reply (``content_kind`` ``assistant_reply``) is kept in the store, not processed:
with a promotion lane, the next operator prompt of the session is joined to it and the words
become a RULING, an OPERATOR-CONSENT or an orchestrator inbox MSG (OMN-19513).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from omnimarket.models.operator_capture import (
    EnumOperatorCaptureStatus,
    ModelCaptureProcessRequest,
    ModelCaptureProcessResult,
    ModelOperatorCaptureReceipt,
    ModelOperatorPromptRecord,
)
from omnimarket.nodes.node_operator_capture_effect.handlers import capture_store
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    DelegateRunner,
    LedgerAppender,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_process import (
    HandlerCaptureProcess,
)

ORIGIN_EVENT = "content-captured"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


class HandlerOperatorCaptureEffect:
    """Take one operator prompt in and run the capture worker; answer with one receipt."""

    def __init__(
        self,
        *,
        store_dir: Path,
        ledger_path: Path,
        host_name: str,
        delegate: DelegateRunner,
        append: LedgerAppender,
        clock: Callable[[], datetime] | None = None,
        max_captures: int = 20,
        promotion_lane: str | None = None,
    ) -> None:
        self._store = store_dir
        self._ledger = ledger_path
        self._host = host_name
        self._delegate = delegate
        self._append = append
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max = max_captures
        self._promotion_lane = promotion_lane

    @property
    def host_name(self) -> str:
        return self._host

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def retry(self) -> ModelCaptureProcessResult:
        """One worker run over whatever the inbox still holds."""
        return HandlerCaptureProcess(
            self._delegate, self._append, now=self._clock()
        ).handle(
            ModelCaptureProcessRequest(
                store_dir=self._store,
                ledger_path=self._ledger,
                max_captures=self._max,
                promotion_lane=self._promotion_lane,
            )
        )

    def handle(self, record: ModelOperatorPromptRecord) -> ModelOperatorCaptureReceipt:
        said_at = (
            _parse_time(record.said_at)
            or _parse_time(record.emitted_at)
            or self._clock()
        )
        if record.is_assistant_reply:
            # Kept so the session's next operator prompt is joined to the question it answered.
            capture_store.remember_reply(
                self._store,
                session_id=record.session_id,
                text=record.content,
                at=said_at,
            )
            return ModelOperatorCaptureReceipt(
                host=self._host,
                session_id=record.session_id,
                prompt_id=record.prompt_id,
                status=EnumOperatorCaptureStatus.REPLY_NOTED,
                result=ModelCaptureProcessResult(),
                answered_at=self._clock(),
            )
        if not record.is_operator_prompt:
            msg = f"not an operator prompt: content_kind={record.content_kind!r}"
            raise ValueError(msg)
        taken = capture_store.ingest(
            self._store,
            session_id=record.session_id,
            text=record.content,
            source=f"bus:{record.actor or 'claude'}",
            received_at=said_at,
            origin_event=ORIGIN_EVENT,
            prompt_id=record.prompt_id,
        )
        result = self.retry()
        if taken is None:
            status = EnumOperatorCaptureStatus.DUPLICATE
        elif result.pending:
            status = EnumOperatorCaptureStatus.PENDING
        else:
            status = EnumOperatorCaptureStatus.RECORDED
        return ModelOperatorCaptureReceipt(
            host=self._host,
            session_id=record.session_id,
            prompt_id=record.prompt_id,
            status=status,
            result=result,
            answered_at=self._clock(),
        )


__all__ = ["ORIGIN_EVENT", "HandlerOperatorCaptureEffect"]
