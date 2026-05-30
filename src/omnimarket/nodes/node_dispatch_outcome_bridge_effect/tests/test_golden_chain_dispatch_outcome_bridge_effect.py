# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden-chain tests for node_dispatch_outcome_bridge_effect."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from omnimarket.nodes.node_dispatch_outcome_bridge_effect.handlers.handler_bridge import (
    SQL_UPSERT_DISPATCH_EVAL_RESULT,
    process_event,
)


class _FakeDb:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((query, *args))
        return "INSERT 0 1"


def _completed_event(
    task_id: str = "t1",
    dispatch_id: str = "d1",
    status: str = "completed",
    token_cost: int = 1000,
    dollars_cost: float = 0.01,
    ticket_id: str | None = "OMN-99999",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "dispatch_id": dispatch_id,
        "ticket_id": ticket_id,
        "status": status,
        "token_cost": token_cost,
        "dollars_cost": dollars_cost,
        "model_calls": [],
        "cost_provenance": {"usage_source": "unknown"},
    }


@pytest.mark.asyncio
async def test_process_event_pass_verdict_inserts_row() -> None:
    db = _FakeDb()
    ok = await process_event(_completed_event(status="completed"), db, None)

    assert ok is True
    assert len(db.calls) == 1
    query, task_id, dispatch_id, *rest = db.calls[0]
    assert SQL_UPSERT_DISPATCH_EVAL_RESULT.strip() in query.strip()
    assert task_id == "t1"
    assert dispatch_id == "d1"
    verdict = rest[1]
    assert verdict == "PASS"


@pytest.mark.asyncio
async def test_process_event_failed_status_writes_fail_verdict() -> None:
    db = _FakeDb()
    ok = await process_event(_completed_event(status="failed"), db, None)

    assert ok is True
    assert len(db.calls) == 1
    verdict = db.calls[0][4]
    assert verdict == "FAIL"


@pytest.mark.asyncio
async def test_process_event_error_status_writes_error_verdict() -> None:
    db = _FakeDb()
    ok = await process_event(_completed_event(status="error"), db, None)

    assert ok is True
    verdict = db.calls[0][4]
    assert verdict == "ERROR"


@pytest.mark.asyncio
async def test_process_event_publishes_to_kafka_when_producer_present() -> None:
    db = _FakeDb()
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()

    ok = await process_event(_completed_event(), db, producer)

    assert ok is True
    producer.send_and_wait.assert_awaited_once()
    call_args = producer.send_and_wait.call_args
    topic = call_args[0][0]
    assert topic == "onex.evt.omniintelligence.dispatch-outcome-evaluated.v1"

    raw_value = call_args[1]["value"]
    published = json.loads(raw_value.decode())
    assert published["task_id"] == "t1"
    assert published["dispatch_id"] == "d1"
    assert published["verdict"] == "PASS"


@pytest.mark.asyncio
async def test_process_event_skips_publish_when_no_producer() -> None:
    db = _FakeDb()
    ok = await process_event(_completed_event(), db, None)
    assert ok is True
    assert len(db.calls) == 1


@pytest.mark.asyncio
async def test_process_event_db_error_returns_false() -> None:
    class _ErrorDb:
        async def execute(self, *args: Any) -> str:
            raise RuntimeError("db connection lost")

    ok = await process_event(_completed_event(), _ErrorDb(), None)
    assert ok is False


@pytest.mark.asyncio
async def test_process_event_malformed_payload_returns_false() -> None:
    ok = await process_event(
        {"task_id": "", "dispatch_id": "", "status": ""}, _FakeDb(), None
    )
    assert ok is False


@pytest.mark.asyncio
async def test_process_event_handle_dispatch_outcome_called_once() -> None:
    db = _FakeDb()
    with patch(
        "omnimarket.nodes.node_dispatch_outcome_bridge_effect.handlers.handler_bridge.handle_dispatch_outcome"
    ) as mock_eval:
        from omniintelligence.nodes.node_dispatch_outcome_eval_effect.models.model_output import (
            ModelOutput,
        )

        mock_eval.return_value = ModelOutput(
            verdict="PASS",
            quality_score=None,
            token_cost=500,
            dollars_cost=0.005,
            model_calls=0,
            usage_source=None,
            estimation_method=None,
            source_payload_hash="a" * 64,
            published_event_id=None,
            evaluated_at=datetime.now(UTC),
            eval_latency_ms=5,
        )
        ok = await process_event(_completed_event(), db, None)

    assert ok is True
    mock_eval.assert_awaited_once()
