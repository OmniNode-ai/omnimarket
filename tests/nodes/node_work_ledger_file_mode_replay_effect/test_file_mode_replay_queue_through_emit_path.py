# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The replay queue publishes through the real work-ledger emit handler, and the seam
counter reads the queue as pending-replay until the row reaches the projection."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
    ModelWorkLedgerEmitRequest,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.handler_file_mode_replay import (
    HandlerFileModeReplay,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.replay_queue import (
    InMemoryReplayQueue,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.models.model_file_mode_replay import (
    EnumReplayOperation,
    ModelFileModeReplayRequest,
    ModelReplayPublishOutcome,
)
from omnimarket.nodes.node_work_ledger_seam_counter_compute.handlers.handler_seam_counter import (
    HandlerSeamCounter,
    publish_topic_from_contract,
)
from omnimarket.nodes.node_work_ledger_seam_counter_compute.handlers.seam_sources import (
    InMemoryRefusals,
    InMemorySeamEventPublisher,
    InMemorySeamSources,
)
from omnimarket.nodes.node_work_ledger_seam_counter_compute.models.model_seam_counter import (
    EnumSeamVerdict,
    EnumSeamWriterPath,
    ModelSeamCounterRequest,
    ModelSeamCounterResult,
    ModelSeamRowRef,
    ModelSeamWindow,
)
from tests.helpers.work_ledger_open_gate import OPEN_JUDGE_GATE

pytestmark = pytest.mark.unit

ROW = "2026-10-31T10:00:00Z | MSG | lane=alpha | from=alpha | to=orchestrator | file mode row"


class _Emitter:
    """The emit-effect spool: published only while the bus is up."""

    def __init__(self) -> None:
        self.up = False
        self.requests: list[ModelEmitRequest] = []

    def handle(self, request: ModelEmitRequest) -> ModelEmitResult:
        self.requests.append(request)
        return ModelEmitResult(event_id=request.event_id, published=self.up)


class _EmitPathPublisher:
    """Adapts the real HandlerWorkLedgerEmit to the replay node's publisher protocol."""

    def __init__(self, emitter: _Emitter) -> None:
        self._handler = HandlerWorkLedgerEmit(
            judge_gate=OPEN_JUDGE_GATE, emitter=emitter
        )

    def publish_row(self, row: str, ledger_id: str) -> ModelReplayPublishOutcome:
        result = self._handler.handle(
            ModelWorkLedgerEmitRequest(row=row, ledger_id=ledger_id)
        )
        return ModelReplayPublishOutcome(
            accepted=result.accepted,
            published=result.published,
            refusal=result.refusal,
            error=result.error,
        )


def test_file_mode_replay_queue_reaches_the_projection_once_the_bus_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    emitter = _Emitter()
    queue = InMemoryReplayQueue()
    replay = HandlerFileModeReplay(queue=queue, publisher=_EmitPathPublisher(emitter))
    row_id = work_ledger_row_id(ROW)
    window = ModelSeamWindow(
        since=datetime(2026, 10, 31, 0, 0, tzinfo=UTC),
        until=datetime(2026, 10, 31, 23, 30, tzinfo=UTC),
    )
    file_row = ModelSeamRowRef(
        row_id=row_id,
        kind="MSG",
        lane="alpha",
        writer_path=EnumSeamWriterPath.FILE_MODE,
    )
    projection: list[str] = []

    def count() -> ModelSeamCounterResult:
        sources = InMemorySeamSources(file=[file_row], topic=[], projection=projection)
        return HandlerSeamCounter(
            sources=sources,
            refusals=InMemoryRefusals([]),
            pending=queue,
            publisher=InMemorySeamEventPublisher(),
            topic=publish_topic_from_contract(),
        ).handle(ModelSeamCounterRequest(correlation_id=uuid4(), window=window))

    # Appended in file mode: not queued yet, so the conservation check fails on it.
    assert count().verdict is EnumSeamVerdict.FAIL

    replay.handle(
        ModelFileModeReplayRequest(operation=EnumReplayOperation.ENQUEUE, rows=[ROW])
    )
    queued = count()
    assert queued.verdict is EnumSeamVerdict.PASS
    assert queued.event.pending_count == 1

    # Bus down: the emit path only spools, so the row stays pending.
    drained = replay.handle(
        ModelFileModeReplayRequest(operation=EnumReplayOperation.DRAIN)
    )
    assert drained.replayed == []
    assert len(drained.still_pending) == 1

    # Bus accepts again: the row is published under its own row id, then it is projected.
    emitter.up = True
    drained = replay.handle(
        ModelFileModeReplayRequest(operation=EnumReplayOperation.DRAIN)
    )
    assert drained.replayed == [row_id]
    assert {r.correlation_id for r in emitter.requests} == {row_id}
    projection.append(row_id)
    final = count()
    assert final.verdict is EnumSeamVerdict.PASS
    assert (final.event.projected_count, final.event.pending_count) == (1, 0)
