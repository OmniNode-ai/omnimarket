# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A judged row is published on its old row topic and once on the accepted-row topic (OMN-25630, L12).

Golden chain: a row the judge sequenced reaches the old topic's event type and the accepted-row event type, with
the same row_id and its sequence. Error chain: a refused accepted-row emit is a typed failure and the retry
re-emits the same event ids. Failing control: a row with no sequence reaches no accepted-row topic.
"""

from __future__ import annotations

import pytest

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.models.model_work_ledger_accepted_row import ModelAcceptedRowEvent
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    resolve_event_type,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    ACCEPTED_ROW_EVENT_TYPE,
    HandlerWorkLedgerEmit,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
    ModelWorkLedgerEmitRequest,
)
from tests.helpers.work_ledger_open_gate import OPEN_JUDGE_GATE

pytestmark = pytest.mark.unit

ROW = "2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work"
ACCEPTED_TOPIC = "onex.evt.omnimarket.work-ledger-row-accepted.v1"


class _Recorder:
    def __init__(self, *, refuse_event_type: str | None = None) -> None:
        self.requests: list[ModelEmitRequest] = []
        self._refuse = refuse_event_type

    def handle(self, request: ModelEmitRequest) -> ModelEmitResult:
        if request.event_type == self._refuse:
            raise RuntimeError("spool refused")
        self.requests.append(request)
        return ModelEmitResult(event_id=request.event_id, published=True)


def test_the_registry_routes_the_accepted_row_event_type_to_the_accepted_row_topic() -> (
    None
):
    (resolved,) = resolve_event_type(ACCEPTED_ROW_EVENT_TYPE)
    assert resolved.topic == ACCEPTED_TOPIC


def test_a_sequenced_row_is_published_on_its_old_topic_and_once_on_the_accepted_row_topic() -> (
    None
):
    recorder = _Recorder()
    result = HandlerWorkLedgerEmit(judge_gate=OPEN_JUDGE_GATE, emitter=recorder).handle(
        ModelWorkLedgerEmitRequest(row=ROW, ledger_seq=12, source="onex-ledger")
    )
    assert result.accepted is True
    old, accepted = recorder.requests
    assert old.event_type == "work.ledger.claim"
    assert accepted.event_type == ACCEPTED_ROW_EVENT_TYPE
    assert old.event_id != accepted.event_id
    event = ModelAcceptedRowEvent.model_validate(accepted.payload)
    assert event.row_id == work_ledger_row_id(ROW) == accepted.correlation_id
    assert (event.ledger_seq, event.source.value, event.raw_row) == (12, "live", ROW)


def test_the_accepted_row_event_id_is_stable_across_a_retry() -> None:
    first, second = _Recorder(), _Recorder()
    for recorder in (first, second):
        HandlerWorkLedgerEmit(judge_gate=OPEN_JUDGE_GATE, emitter=recorder).handle(
            ModelWorkLedgerEmitRequest(row=ROW, ledger_seq=12)
        )
    assert [r.event_id for r in first.requests] == [r.event_id for r in second.requests]


def test_a_refused_accepted_row_emit_is_a_typed_failure_the_caller_retries() -> None:
    recorder = _Recorder(refuse_event_type=ACCEPTED_ROW_EVENT_TYPE)
    result = HandlerWorkLedgerEmit(judge_gate=OPEN_JUDGE_GATE, emitter=recorder).handle(
        ModelWorkLedgerEmitRequest(row=ROW, ledger_seq=12)
    )
    assert result.accepted is False
    assert result.error is not None
    assert "spool refused" in result.error
    assert [r.event_type for r in recorder.requests] == ["work.ledger.claim"]


def test_failing_control_a_row_with_no_sequence_reaches_no_accepted_row_topic() -> None:
    recorder = _Recorder()
    result = HandlerWorkLedgerEmit(judge_gate=OPEN_JUDGE_GATE, emitter=recorder).handle(
        ModelWorkLedgerEmitRequest(row=ROW)
    )
    assert result.accepted is True
    assert [r.event_type for r in recorder.requests] == ["work.ledger.claim"]
