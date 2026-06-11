"""Real-dispatch-path test for node_projection_llm_cost (OMN-13001).

Handler-isolation tests alone are insufficient (standing policy: live can fail
while isolated handlers pass). This test drives the DEPLOYED runtime writer
``LlmCostProjectionRunner`` through the real ``BaseProjectionRunner._handle_message``
dispatch path: a wrapped ``llm-call-completed`` envelope is unwrapped, routed to
``project_event``, and proven to produce one ``INSERT INTO llm_call_metrics`` with
the correct columns and bound values — exactly what the live Kafka consumer does.

The Kafka consumer and Postgres pool are the only seams replaced (a fake message
and a capturing async DB); the unwrap + dispatch + row-build + SQL-bind path under
test is the production code path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from omnimarket.nodes.node_projection_llm_cost.handlers.handler_llm_cost import (
    CONFLICT_KEY,
    TABLE,
    LlmCostProjectionRunner,
)
from omnimarket.nodes.node_projection_llm_cost.handlers.row_llm_call_metrics import (
    LLM_CALL_METRICS_COLUMNS,
)


class _CapturingDb:
    """Stand-in for AsyncpgAdapter that records execute() calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return []


@dataclass
class _FakeMessage:
    topic: str
    partition: int
    offset: int
    value: bytes


def _runner_with_fake_db() -> tuple[LlmCostProjectionRunner, _CapturingDb]:
    runner = LlmCostProjectionRunner()
    fake_db = _CapturingDb()
    runner._db = fake_db  # type: ignore[assignment]
    return runner, fake_db


CANONICAL_EVENT: dict[str, Any] = {
    "correlation_id": "d498ad36-0000-0000-0000-000000000001",
    "model_id": "claude-sonnet-4-6",
    "prompt_tokens": 28416,
    "completion_tokens": 55,
    "total_tokens": 28471,
    "estimated_cost_usd": 0.17897125,
    "latency_ms": 19820,
    "usage_source": "MEASURED",
    "reporting_source": "ab-compare",
    "session_id": "b45a01ea-0000-0000-0000-000000000001",
    "emitted_at": "2026-05-02T19:35:51.171478+00:00",
}


@pytest.mark.asyncio
async def test_envelope_through_real_dispatch_writes_llm_call_metrics_row() -> None:
    runner, fake_db = _runner_with_fake_db()
    topic = runner.topics[0]
    envelope = {"payload": CANONICAL_EVENT, "event_type": "llm-call-completed"}
    msg = _FakeMessage(
        topic=topic,
        partition=0,
        offset=42,
        value=json.dumps(envelope).encode(),
    )

    # The real runtime dispatch method: parse + unwrap + route + project + watermark.
    await runner._handle_message(msg)

    insert_calls = [c for c in fake_db.calls if f"INSERT INTO {TABLE}" in c[0]]
    assert len(insert_calls) == 1, f"expected one {TABLE} insert, got {fake_db.calls}"

    query, params = insert_calls[0]
    assert f"ON CONFLICT ({CONFLICT_KEY}) DO NOTHING" in query
    assert "$12::jsonb" in query
    assert len(params) == len(LLM_CALL_METRICS_COLUMNS)

    # correlation_id, model_id, tokens, cost, honest usage_source land correctly.
    correlation_id, session_id, run_id, model_id = params[0:4]
    prompt_tokens, completion_tokens, total_tokens = params[4:7]
    estimated_cost_usd, latency_ms = params[7:9]
    usage_source, usage_is_estimated = params[9:11]
    assert correlation_id == "d498ad36-0000-0000-0000-000000000001"
    assert model_id == "claude-sonnet-4-6"
    assert prompt_tokens == 28416
    assert completion_tokens == 55
    assert total_tokens == 28471
    assert estimated_cost_usd == pytest.approx(0.17897125)
    assert latency_ms == pytest.approx(19820.0)
    assert usage_source == "API"  # MEASURED -> API
    assert usage_is_estimated is False
    assert session_id == "b45a01ea-0000-0000-0000-000000000001"
    assert run_id is None


@pytest.mark.asyncio
async def test_project_event_returns_true_and_binds_input_hash() -> None:
    runner, fake_db = _runner_with_fake_db()
    from omnimarket.projection.runner import MessageMeta

    meta = MessageMeta(partition=0, offset=1, fallback_id="fb")
    ok = await runner.project_event(runner.topics[0], dict(CANONICAL_EVENT), meta)
    assert ok is True

    _query, params = fake_db.calls[0]
    input_hash = params[12]  # 13th column == input_hash
    assert isinstance(input_hash, str)
    assert len(input_hash) == 64
