# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ruling register read: ``handle(ModelRulingReadRequest) -> ModelRulingReadResult`` (OMN-24478).

Reads rows of the standing-rulings register concurrently through the port, returning
readings and emitting events in row order: ``answered`` (golden) for an OK reading,
``refused`` (error) for UNREAD.
The port never raises and a refused read is never an answer; judging the readings into
verdicts belongs to ``node_ruling_register_compute``. A journal write that fails costs
its row's event only: the remaining rows still read, and the failed rows come back in
``sink_errors``. The topics are a constructor parameter of two strings -- the CLI passes
them from the contract -- so no topic string is in this file.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
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
MAX_CONCURRENT_READS = 8


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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._port = port
        self._sink = sink
        self._topics = dict(topics)
        self._clock = clock

    def handle(self, request: ModelRulingReadRequest) -> ModelRulingReadResult:
        readings = []
        sink_errors = []
        rows = request.policy.rows
        with ThreadPoolExecutor(
            max_workers=max(1, min(len(rows), MAX_CONCURRENT_READS))
        ) as executor:
            for row, reading in zip(
                rows, executor.map(self._port.read, rows), strict=True
            ):
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
