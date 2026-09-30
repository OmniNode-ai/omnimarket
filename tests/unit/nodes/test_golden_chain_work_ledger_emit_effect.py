# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_work_ledger_emit_effect and node_projection_work_ledger (OMN-19513).

One ledger row, end to end with zero external infrastructure: the row goes
through the emit node, the real emit-effect spool and enrichment, a recording
publish adapter, and the message the adapter received (enriched envelope and all)
is handed to the projection writer with a database double. What the writer would
persist is asserted, so the chain is proven from the appended line to the SQL.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import JsonType
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    WorkLedgerProjectionWriter,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
    ModelWorkLedgerEmitRequest,
)

pytestmark = pytest.mark.unit

_ROWS = [
    "2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work",
    "2026-09-28T10:10:00Z | HOLD | lane=alpha | id=2026-09-28T10:10:00Z-alpha | surface=lab-dev | until=2026-09-28T12:00:00Z | reserved",
    "2026-09-28T10:15:00Z | MSG | from=alpha | to=beta | id=2026-09-28T10:15:00Z-alpha | please look",
]


class _RecordingAdapter:
    def __init__(self) -> None:
        self.received: list[tuple[str, dict[str, Any], str | None]] = []

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None,
        timeout_seconds: float | None = None,
    ) -> None:
        assert isinstance(payload, dict)
        self.received.append((topic, json.loads(json.dumps(payload)), key))


class _FakeDb:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def execute(self, sql: str, *args: Any) -> None:
        self.statements.append((sql, args))


def test_row_to_bus_to_projection_sql(tmp_path: Path) -> None:
    adapter = _RecordingAdapter()
    handler = HandlerWorkLedgerEmit(
        emitter=HandlerEventEmitEffect(
            spool=SpoolOutbox(tmp_path / "spool"), publish_adapter=adapter
        )
    )
    for row in _ROWS:
        assert handler.handle(ModelWorkLedgerEmitRequest(row=row)).published is True

    assert [t for t, _, _ in adapter.received] == [
        "onex.evt.omnimarket.work-ledger-claim.v1",
        "onex.evt.omnimarket.work-ledger-hold.v1",
        "onex.evt.omnimarket.work-ledger-msg.v1",
    ]
    assert {k for _, _, k in adapter.received} == {"rolling-work-ledger"}

    db = _FakeDb()
    writer = WorkLedgerProjectionWriter()
    writer._db = db  # type: ignore[assignment]
    for topic, payload, _key in adapter.received:
        writer.handle({**payload, "_topic": topic})

    entity_keys = [args[0] for sql, args in db.statements if "work_ledger_state" in sql]
    assert entity_keys == [
        "claim:alpha",
        "hold:2026-09-28T10:10:00Z-alpha",
        "msg:2026-09-28T10:15:00Z-alpha",
    ]
    logged = [args[6] for sql, args in db.statements if "work_ledger_rows" in sql]
    assert logged == _ROWS
