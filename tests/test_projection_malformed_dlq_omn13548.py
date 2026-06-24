"""OMN-13548 (D-03): malformed projection events route to the DLQ topic.

A deliberately malformed delegation / savings event must NOT be logged + dropped
silently. It must emit a DURABLE failure signal on the bus: the offending
envelope published to the contract-declared DLQ topic
(``event_bus.dlq_topics``), carrying the event's correlation_id + the failure
reason. These tests prove the DLQ row exists for that correlation_id.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

DELEGATION_DLQ_TOPIC = "onex.dlq.omnimarket.projection-delegation-malformed.v1"
SAVINGS_DLQ_TOPIC = "onex.dlq.omnimarket.projection-savings-malformed.v1"


def _capture() -> tuple[list[tuple[str, bytes]], Any]:
    published: list[tuple[str, bytes]] = []

    async def capture_publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    return published, capture_publish


def _mock_db() -> Any:
    mock_db = MagicMock(spec=AsyncpgAdapter)
    mock_db.execute = AsyncMock(return_value=None)
    return mock_db


class TestDelegationMalformedDLQ:
    def test_contract_declares_dlq_topic(self) -> None:
        runner = DelegationProjectionRunner()
        assert runner._dlq_topics == [DELEGATION_DLQ_TOPIC]

    def test_malformed_judge_verdict_routes_to_dlq(self) -> None:
        published, capture = _capture()
        runner = DelegationProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        topic = runner._topic_judge_verdict
        # Missing every required field for ModelDelegationJudgeVerdictEvent.
        data: dict[str, Any] = {"correlation_id": "corr-bad-verdict"}
        meta = MessageMeta(partition=0, offset=0, fallback_id="corr-bad-verdict")

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True  # offset committed; not reprocessed in a hot loop
        dlq_rows = [(t, v) for t, v in published if t == DELEGATION_DLQ_TOPIC]
        assert len(dlq_rows) == 1
        envelope = json.loads(dlq_rows[0][1].decode("utf-8"))
        assert envelope["correlation_id"] == "corr-bad-verdict"
        assert envelope["handler"] == "node_projection_delegation"
        assert "validation" in envelope["failure_reason"].lower()
        assert envelope["original_message"] == data

    def test_task_delegated_missing_required_fields_routes_to_dlq(self) -> None:
        published, capture = _capture()
        runner = DelegationProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        topic = runner._topic_delegated
        # task_type / delegated_to absent -> malformed task-delegated event.
        data: dict[str, Any] = {"correlation_id": "corr-bad-delegated"}
        meta = MessageMeta(partition=0, offset=1, fallback_id="corr-bad-delegated")

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == DELEGATION_DLQ_TOPIC]
        assert len(dlq_rows) == 1
        envelope = json.loads(dlq_rows[0][1].decode("utf-8"))
        assert envelope["correlation_id"] == "corr-bad-delegated"
        assert "missing required fields" in envelope["failure_reason"]

    def test_malformed_delegate_skill_terminal_routes_to_dlq(self) -> None:
        published, capture = _capture()
        runner = DelegationProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        topic = runner._topic_delegate_skill_completed
        # status/metrics absent -> ModelDelegateSkillTerminalProjection rejects it.
        data: dict[str, Any] = {"correlation_id": "corr-bad-terminal"}
        meta = MessageMeta(partition=0, offset=2, fallback_id="corr-bad-terminal")

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == DELEGATION_DLQ_TOPIC]
        assert len(dlq_rows) == 1
        envelope = json.loads(dlq_rows[0][1].decode("utf-8"))
        assert envelope["correlation_id"] == "corr-bad-terminal"

    def test_wellformed_event_does_not_route_to_dlq(self) -> None:
        published, capture = _capture()
        runner = DelegationProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        topic = runner._topic_delegated
        data: dict[str, Any] = {
            "correlation_id": "corr-good",
            "task_type": "code-review",
            "delegated_to": "agent-alpha",
        }
        meta = MessageMeta(partition=0, offset=3, fallback_id="corr-good")

        asyncio.run(runner.project_event(topic, data, meta))

        dlq_rows = [(t, v) for t, v in published if t == DELEGATION_DLQ_TOPIC]
        assert dlq_rows == []


class TestSavingsMalformedDLQ:
    def test_contract_declares_dlq_topic(self) -> None:
        runner = SavingsProjectionRunner()
        assert runner._dlq_topics == [SAVINGS_DLQ_TOPIC]

    def test_malformed_savings_estimated_routes_to_dlq(self) -> None:
        published, capture = _capture()
        runner = SavingsProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        # savings-estimated path (not a delegate-skill terminal topic); missing
        # model identifiers -> malformed.
        topic = "onex.evt.omnibase-infra.savings-estimated.v1"
        data: dict[str, Any] = {
            "session_id": "sess-bad-savings",
            "event_timestamp": "2026-04-29T12:00:00+00:00",
        }
        meta = MessageMeta(partition=0, offset=0, fallback_id="sess-bad-savings")

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == SAVINGS_DLQ_TOPIC]
        assert len(dlq_rows) == 1
        envelope = json.loads(dlq_rows[0][1].decode("utf-8"))
        assert envelope["correlation_id"] == "sess-bad-savings"
        assert envelope["handler"] == "node_projection_savings"
        assert envelope["original_message"] == data

    def test_inconsistent_savings_routes_to_dlq(self) -> None:
        published, capture = _capture()
        runner = SavingsProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        topic = "onex.evt.omnibase-infra.savings-estimated.v1"
        # savings_usd != cloud - local -> inconsistent.
        data: dict[str, Any] = {
            "session_id": "sess-inconsistent",
            "event_timestamp": "2026-04-29T12:00:00+00:00",
            "model_local": "qwen3",
            "model_cloud_baseline": "claude-opus-4",
            "local_cost_usd": "1.0",
            "cloud_cost_usd": "3.0",
            "savings_usd": "5.0",
        }
        meta = MessageMeta(partition=0, offset=1, fallback_id="sess-inconsistent")

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == SAVINGS_DLQ_TOPIC]
        assert len(dlq_rows) == 1
        envelope = json.loads(dlq_rows[0][1].decode("utf-8"))
        assert envelope["correlation_id"] == "sess-inconsistent"
        assert "inconsistent savings" in envelope["failure_reason"]

    def test_malformed_delegate_skill_terminal_routes_to_dlq(self) -> None:
        published, capture = _capture()
        runner = SavingsProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        topic = runner._topic_delegate_skill_completed
        data: dict[str, Any] = {"correlation_id": "corr-bad-skill-savings"}
        meta = MessageMeta(partition=0, offset=2, fallback_id="corr-bad-skill-savings")

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == SAVINGS_DLQ_TOPIC]
        assert len(dlq_rows) == 1
        envelope = json.loads(dlq_rows[0][1].decode("utf-8"))
        assert envelope["correlation_id"] == "corr-bad-skill-savings"
