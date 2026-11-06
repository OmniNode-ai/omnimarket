# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work ledger read node bounds every answer under the broker's limit.

The recorded case: on the dev lane a ``newest-per-lane`` query with no lane,
since or limit answered 73 MB, the broker refused the record, and the dead
letter replay redelivered the command until the query consumer jammed (09:34Z
to about 10:31Z on 2026-11-06). The case is rebuilt here with the same shape:
thousands of lanes, each with a newest row of several KB, so the unbounded
answer measures the recorded 73 MB. Nothing here reaches a broker.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerAnswerBound,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_answer_bound import (
    answer_size_bytes,
    load_work_ledger_answer_bound,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    ProtocolWorkLedgerQueryReader,
)
from tests.test_golden_chain_work_ledger_query import (
    CONTRACT,
    FakeReader,
    fixture_records,
)

pytestmark = pytest.mark.unit

#: The dev bus's producer limit, measured 2026-10 (OMN-14498) and read again
#: from the producer config default.
BROKER_MAX_REQUEST_SIZE = 1_048_588
RECORDED_ANSWER_BYTES = 73_000_000
NOW = datetime(2026, 11, 6, 9, 30, tzinfo=UTC)
LANES = 3_000
ROW_BODY_CHARS = 12_000


def recorded_case_records() -> tuple[ModelWorkLedgerRowRecord, ...]:
    """One newest row per lane, sized so the unbounded answer is the recorded 73 MB."""
    body = "x" * ROW_BODY_CHARS
    records = []
    for index in range(LANES):
        stamp = NOW - timedelta(seconds=LANES - index)
        raw = (
            f"{stamp:%Y-%m-%dT%H:%M:%SZ} | STATUS | lane=recorded-lane-{index:05d} "
            f"| ticket=OMN-22478 | {body}"
        )
        records.append(
            ModelWorkLedgerRowRecord(
                row_id=work_ledger_row_id(raw),
                row_ts=stamp,
                row_type="STATUS",
                row_lane=f"recorded-lane-{index:05d}",
                text=raw,
                raw_row=raw,
                source="onex-ledger",
                projected_at=stamp,
            )
        )
    return tuple(records)


@pytest.fixture(scope="module")
def recorded_reader() -> FakeReader:
    return FakeReader(recorded_case_records())


def _node(reader: FakeReader) -> HandlerWorkLedgerQuery:
    return HandlerWorkLedgerQuery(
        reader=cast(ProtocolWorkLedgerQueryReader, reader), now=lambda: NOW
    )


def _newest_per_lane(**fields: object) -> ModelWorkLedgerQueryRequest:
    return ModelWorkLedgerQueryRequest.model_validate(
        {
            "correlation_id": uuid4(),
            "query": EnumWorkLedgerQueryKind.NEWEST_PER_LANE,
            "now": NOW,
            **fields,
        }
    )


def _record_bytes(result: ModelWorkLedgerQueryResult) -> int:
    """The record the producer would be handed, envelope included."""
    envelope = ModelEventEnvelope[dict[str, object]](
        payload=result.model_dump(mode="json"), correlation_id=result.correlation_id
    )
    return len(envelope.model_dump_json().encode("utf-8"))


# ---- golden ---------------------------------------------------------------


def test_golden_newest_per_lane_honours_its_limit() -> None:
    reader = FakeReader(fixture_records())
    everything = _node(reader).handle(_newest_per_lane())
    limited = _node(reader).handle(_newest_per_lane(limit=3))
    assert everything.matched == 7
    assert limited.matched == 7
    assert len(limited.rows) == 3
    assert limited.rows == everything.rows[-3:]


def test_golden_recorded_case_is_refused_with_the_bound_and_the_bounded_form(
    recorded_reader: FakeReader,
) -> None:
    result = _node(recorded_reader).handle(_newest_per_lane())
    bound = load_work_ledger_answer_bound(CONTRACT)
    assert result.status == "refused"
    assert result.error is None
    assert result.rows == ()
    assert result.matched == LANES
    refusal = result.refusal
    assert refusal is not None
    assert refusal.code == "answer_too_large"
    assert refusal.bound_bytes == bound.max_answer_bytes
    assert refusal.answer_bytes >= RECORDED_ANSWER_BYTES
    assert refusal.suggested_limit is not None
    assert f"limit={refusal.suggested_limit}" in refusal.suggestion
    assert str(bound.max_answer_bytes) in refusal.suggestion
    assert answer_size_bytes(result) < bound.max_answer_bytes
    assert _record_bytes(result) < BROKER_MAX_REQUEST_SIZE


def test_golden_the_suggested_limit_answers_under_the_bound(
    recorded_reader: FakeReader,
) -> None:
    refused = _node(recorded_reader).handle(_newest_per_lane())
    assert refused.refusal is not None
    limit = refused.refusal.suggested_limit
    assert limit is not None
    answered = _node(recorded_reader).handle(_newest_per_lane(limit=limit))
    bound = load_work_ledger_answer_bound(CONTRACT)
    assert answered.status == "ok"
    assert answered.refusal is None
    assert len(answered.rows) == limit
    assert answered.matched == LANES
    assert answer_size_bytes(answered) <= bound.max_answer_bytes
    assert _record_bytes(answered) < BROKER_MAX_REQUEST_SIZE


def test_golden_rows_query_over_the_bound_is_refused_too(
    recorded_reader: FakeReader,
) -> None:
    request = ModelWorkLedgerQueryRequest(
        correlation_id=uuid4(), query=EnumWorkLedgerQueryKind.ROWS, now=NOW
    )
    result = _node(recorded_reader).handle(request)
    assert result.status == "refused"
    assert result.refusal is not None
    assert "limit=" in result.refusal.suggestion
    assert "lane, ticket, pr, kinds, since or until" in result.refusal.suggestion


def test_golden_a_small_answer_is_untouched() -> None:
    result = _node(FakeReader(fixture_records())).handle(_newest_per_lane())
    assert result.status == "ok"
    assert result.refusal is None
    assert len(result.rows) == 7


def test_golden_real_runtime_dispatch_publishes_the_refusal_under_the_broker_limit(
    recorded_reader: FakeReader,
) -> None:
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )

    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors
    route = manifest.contracts[0].handler_routing.handlers[0]
    callback = _make_dispatch_callback(
        _node(recorded_reader), event_model=route.event_model
    )
    request = _newest_per_lane()
    dispatch = asyncio.run(
        callback(
            {
                "payload": request.model_dump(mode="json"),
                "correlation_id": str(request.correlation_id),
            }
        )
    )
    assert dispatch is not None
    terminal = ModelWorkLedgerQueryResult.model_validate(dispatch.output_events[0])
    assert terminal.status == "refused"
    assert _record_bytes(terminal) < BROKER_MAX_REQUEST_SIZE


# ---- error chain ----------------------------------------------------------


def test_error_a_refusal_is_a_terminal_answer_and_never_retryable(
    recorded_reader: FakeReader,
) -> None:
    """Returned, not raised: the runtime dead-letters a raise, and the replay
    consumer redelivers a dead letter. A returned answer has no dead letter."""
    node = _node(recorded_reader)
    first = node.handle(_newest_per_lane())
    second = node.handle(_newest_per_lane())
    assert first.refusal is not None
    assert second.refusal is not None
    assert first.refusal.retryable is False
    assert first.refusal.model_dump() == second.refusal.model_dump()


def test_error_a_refusal_cannot_claim_to_be_retryable() -> None:
    with pytest.raises(ValidationError):
        ModelWorkLedgerQueryResult.model_validate(
            {
                "correlation_id": str(uuid4()),
                "query": "newest-per-lane",
                "window_since": None,
                "window_until": NOW.isoformat(),
                "status": "refused",
                "refusal": {
                    "code": "answer_too_large",
                    "retryable": True,
                    "bound_bytes": 1,
                    "answer_bytes": 2,
                    "matched": 0,
                    "suggestion": "narrow it",
                },
            }
        )


def test_error_a_database_error_is_still_an_error_not_a_refusal() -> None:
    reader = FakeReader(fixture_records(), RuntimeError("database offline"))
    result = _node(reader).handle(_newest_per_lane())
    assert result.status == "error"
    assert result.refusal is None
    assert result.error == "RuntimeError: database offline"


def test_error_a_bound_that_does_not_fit_under_the_broker_does_not_load() -> None:
    with pytest.raises(ValidationError, match="exceeds broker_max_request_size"):
        ModelWorkLedgerAnswerBound(
            broker_max_request_size=1_000,
            envelope_headroom_bytes=100,
            max_answer_bytes=901,
        )


def test_error_the_contract_bound_fits_under_the_broker_limit() -> None:
    bound = load_work_ledger_answer_bound(CONTRACT)
    assert bound.broker_max_request_size == BROKER_MAX_REQUEST_SIZE
    assert bound.max_answer_bytes + bound.envelope_headroom_bytes <= (
        BROKER_MAX_REQUEST_SIZE
    )


def test_error_a_contract_without_a_bound_does_not_load(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONTRACT.read_text())
    del raw["work_ledger_query"]["answer_bound"]
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="answer_bound"):
        load_work_ledger_answer_bound(path)
