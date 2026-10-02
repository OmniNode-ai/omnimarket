# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the work-ledger emit handler and its spool path (OMN-19513)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from omnimarket.events.enum_ledger_row_type import (
    EnumLedgerRowType,
)
from omnimarket.events.model_ledger_row_event import (
    ModelLedgerRowEvent,
    work_ledger_event_id,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
    ModelWorkLedgerEmitRequest,
)

pytestmark = pytest.mark.unit


class _Recorder:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.requests: list[ModelEmitRequest] = []
        self._raises = raises

    def handle(self, request: ModelEmitRequest) -> ModelEmitResult:
        if self._raises is not None:
            raise self._raises
        self.requests.append(request)
        return ModelEmitResult(event_id=request.event_id, published=True)


class _Adapter:
    def __init__(self) -> None:
        self.published: list[tuple[str, JsonType, str | None]] = []

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
        self.published.append((topic, payload, key))


def test_every_type_is_emitted_on_its_own_event_type(rows: dict[str, str]) -> None:
    recorder = _Recorder()
    handler = HandlerWorkLedgerEmit(emitter=recorder)
    for type_cell, row in rows.items():
        result = handler.handle(ModelWorkLedgerEmitRequest(row=row))
        row_type = EnumLedgerRowType(type_cell)
        assert result.accepted is True
        assert result.topic == row_type.topic
        assert result.event_type == row_type.event_type
    assert [r.event_type for r in recorder.requests] == [
        EnumLedgerRowType(t).event_type for t in rows
    ]


def test_payload_carries_the_exact_row(rows: dict[str, str]) -> None:
    recorder = _Recorder()
    HandlerWorkLedgerEmit(emitter=recorder).handle(
        ModelWorkLedgerEmitRequest(row=rows["CLAIM"])
    )
    payload = recorder.requests[0].payload
    assert isinstance(payload, dict)
    assert payload["raw_row"] == rows["CLAIM"]
    assert payload["ledger_id"] == "rolling-work-ledger"
    assert recorder.requests[0].event_id == str(
        work_ledger_event_id(str(payload["ledger_id"]), str(payload["row_id"]))
    )


def test_a_row_that_cannot_be_typed_spools_nothing() -> None:
    recorder = _Recorder()
    result = HandlerWorkLedgerEmit(emitter=recorder).handle(
        ModelWorkLedgerEmitRequest(row="2026-09-28T10:00:00Z | NOTE | lane=x | hi")
    )
    assert result.accepted is False
    assert result.refusal is not None
    assert recorder.requests == []


def test_a_spool_refusal_is_a_typed_result_not_a_raise(rows: dict[str, str]) -> None:
    handler = HandlerWorkLedgerEmit(
        emitter=_Recorder(raises=RuntimeError("spool full"))
    )
    result = handler.handle(ModelWorkLedgerEmitRequest(row=rows["MSG"]))
    assert result.accepted is False
    assert result.error is not None
    assert "spool full" in result.error
    assert result.topic == EnumLedgerRowType.MSG.topic


def test_through_the_real_spool_the_event_is_durable_and_typed(
    tmp_path: Path, rows: dict[str, str]
) -> None:
    """Spool-only (no broker): the row waits on disk on its topic at duty_critical."""
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerWorkLedgerEmit(emitter=HandlerEventEmitEffect(spool=spool))
    result = handler.handle(ModelWorkLedgerEmitRequest(row=rows["HOLD"]))
    assert result.accepted is True
    assert result.published is False
    files = sorted((tmp_path / "spool").glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["topic"] == EnumLedgerRowType.HOLD.topic
    # What the fold receives (post-enrichment) still validates as the typed event.
    event = TypeAdapter(ModelLedgerRowEvent).validate_python(record["payload"])
    assert event.raw_row == rows["HOLD"]


def test_through_a_recording_adapter_the_event_is_keyed_by_ledger(
    tmp_path: Path, rows: dict[str, str]
) -> None:
    adapter = _Adapter()
    handler = HandlerWorkLedgerEmit(
        emitter=HandlerEventEmitEffect(
            spool=SpoolOutbox(tmp_path / "spool"), publish_adapter=adapter
        )
    )
    result = handler.handle(ModelWorkLedgerEmitRequest(row=rows["STATUS"]))
    assert result.published is True
    topic, _payload, key = adapter.published[0]
    assert topic == EnumLedgerRowType.STATUS.topic
    assert key == "rolling-work-ledger"
