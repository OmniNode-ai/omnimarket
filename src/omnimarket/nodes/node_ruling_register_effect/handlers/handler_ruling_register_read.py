# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ruling register read: ``handle(ModelRulingReadRequest) -> ModelRulingReadResult`` (OMN-24478).

Reads each row of the standing-rulings register in order through the port and emits one
event per row: ``answered`` (golden) for an OK reading, ``refused`` (error) for UNREAD.
The port never raises and a refused read is never an answer; judging the readings into
verdicts belongs to ``node_ruling_register_compute``. A journal write that fails costs
its row's event only: the remaining rows still read, and the failed rows come back in
``sink_errors``. The whole read is bounded by the constructor's ``total_timeout_seconds``:
a row the budget did not reach reads UNREAD and still emits its refused event. The topics
are a constructor parameter of two strings -- the CLI passes them from the contract -- so
no topic string is in this file.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.model_ruling_register import (
    EnumReadStatus,
    ModelRulingReading,
    ModelRulingReadRequest,
    ModelRulingReadResult,
    ModelRulingRow,
)
from omnimarket.nodes.node_ruling_register_effect.models import (
    ModelRulingReadEvent,
)
from omnimarket.nodes.node_ruling_register_effect.protocols import (
    ProtocolRulingEventSink,
    ProtocolRulingReadPort,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerRulingRegisterRead:
    """``handle(ModelRulingReadRequest) -> ModelRulingReadResult``."""

    def __init__(
        self,
        port: ProtocolRulingReadPort,
        sink: ProtocolRulingEventSink,
        topics: Mapping[str, str],
        *,
        total_timeout_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._port = port
        self._sink = sink
        self._topics = dict(topics)
        self._total_timeout_seconds = total_timeout_seconds
        self._clock = clock
        self._monotonic = monotonic

    def handle(self, request: ModelRulingReadRequest) -> ModelRulingReadResult:
        readings = []
        sink_errors = []
        start = self._monotonic()
        for row in request.policy.rows:
            remaining = self._total_timeout_seconds - (self._monotonic() - start)
            if remaining < 1:
                # The budget is spent: the row is neither read nor dropped. It reads
                # UNREAD and still emits its refused event like any unread row.
                reading = ModelRulingReading(
                    row_id=row.row_id,
                    status=EnumReadStatus.UNREAD,
                    read_at=self._clock(),
                    detail=(
                        f"not read: the register's {self._total_timeout_seconds} "
                        "second read budget was spent"
                    ),
                )
            else:
                reading = self._port.read(row, budget_seconds=int(remaining))
            readings.append(reading)
            try:
                self._emit(request, row, reading)
            except OSError as exc:
                # A journal write that fails loses its row's event, not the
                # remaining rows' reads; the failures come back by row.
                sink_errors.append(f"{row.row_id}: {type(exc).__name__}")
        return ModelRulingReadResult(
            correlation_id=request.correlation_id,
            readings=tuple(readings),
            sink_errors=tuple(sink_errors),
        )

    def _emit(
        self,
        request: ModelRulingReadRequest,
        row: ModelRulingRow,
        reading: ModelRulingReading,
    ) -> None:
        answered = reading.status is EnumReadStatus.OK
        event = ModelRulingReadEvent(
            kind="answered" if answered else "refused",
            topic=self._topics[
                "ruling_read_answered" if answered else "ruling_read_refused"
            ],
            row_id=row.row_id,
            ruling=row.ruling,
            status=reading.status,
            exit_code=reading.exit_code,
            detail=reading.detail,
            read_at=reading.read_at,
            correlation_id=request.correlation_id,
        )
        self._sink.emit(event)


__all__ = ["HandlerRulingRegisterRead", "load_topics"]
