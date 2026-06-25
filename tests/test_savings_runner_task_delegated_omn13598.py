# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13598: deployed SavingsProjectionRunner must materialize savings_estimates
from task-delegated.v1 events.

TDD gate: these tests FAIL on the unfixed handler_savings.SavingsProjectionRunner
and PASS after the fix. The deployed catalog binding
(omnibase_infra/docker/catalog/services/omnimarket-projection-savings.yaml)
points at handler_savings, not handler_projection_savings — so the fix must land
in handler_savings.SavingsProjectionRunner.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.pricing import build_premium_counterfactual
from omnimarket.projection.runner import MessageMeta

TASK_DELEGATED_TOPIC = "onex.evt.omniclaude.task-delegated.v1"
SAVINGS_DLQ_TOPIC = "onex.dlq.omnimarket.projection-savings-malformed.v1"

CORR_ID = "2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f82"


def _capture() -> tuple[list[tuple[str, bytes]], Any]:
    published: list[tuple[str, bytes]] = []

    async def capture_publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    return published, capture_publish


def _mock_db() -> Any:
    mock_db = MagicMock(spec=AsyncpgAdapter)
    mock_db.execute = AsyncMock(return_value=None)
    return mock_db


def _task_delegated_payload(
    *, cost_usd: str = "0.003", with_counterfactual: bool = True
) -> dict[str, Any]:
    """Canonical task-delegated.v1 payload with a pinned premium counterfactual."""
    payload: dict[str, Any] = {
        "correlation_id": CORR_ID,
        "task_type": "code_generation",
        "delegated_to": "cheap-cloud-glm",
        "model_name": "glm-5.2",
        "repo": "omnimarket",
        "cost_usd": cost_usd,
        "timestamp": "2026-06-20T12:00:00+00:00",
    }
    if with_counterfactual:
        cf = build_premium_counterfactual(prompt_tokens=1000, completion_tokens=500)
        assert cf is not None
        payload["premium_counterfactual"] = cf.model_dump(mode="json")
    return payload


@pytest.mark.unit
class TestSavingsRunnerTaskDelegatedV1:
    """SavingsProjectionRunner (deployed handler) processes task-delegated.v1."""

    def test_task_delegated_topic_is_in_subscribe_topics(self) -> None:
        """Contract must declare task-delegated.v1 as a subscribed topic."""
        runner = SavingsProjectionRunner()
        assert TASK_DELEGATED_TOPIC in runner.subscribe_topics, (
            f"Expected {TASK_DELEGATED_TOPIC!r} in subscribe_topics; "
            f"got {runner.subscribe_topics}"
        )

    def test_task_delegated_with_counterfactual_upserts_savings_row(self) -> None:
        """A real task-delegated event with a counterfactual must upsert a row,
        not be DLQ'd as malformed."""
        published, capture = _capture()
        runner = SavingsProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        data = _task_delegated_payload()
        meta = MessageMeta(partition=0, offset=0, fallback_id=CORR_ID)

        ok = asyncio.run(runner.project_event(TASK_DELEGATED_TOPIC, data, meta))

        assert ok is True, "project_event must return True (offset committed)"
        dlq_rows = [(t, v) for t, v in published if t == SAVINGS_DLQ_TOPIC]
        assert dlq_rows == [], (
            f"task-delegated.v1 with counterfactual must NOT be DLQ'd; "
            f"got DLQ entries: {[json.loads(v) for _, v in dlq_rows]}"
        )
        # DB upsert must have been called exactly once.
        runner._db.execute.assert_awaited_once()

    def test_task_delegated_without_counterfactual_is_truthful_empty(self) -> None:
        """No counterfactual = no defensible saving = no row, no DLQ."""
        published, capture = _capture()
        runner = SavingsProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        data = _task_delegated_payload(with_counterfactual=False)
        meta = MessageMeta(partition=0, offset=1, fallback_id=CORR_ID)

        ok = asyncio.run(runner.project_event(TASK_DELEGATED_TOPIC, data, meta))

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == SAVINGS_DLQ_TOPIC]
        assert dlq_rows == [], "no-counterfactual task-delegated must NOT be DLQ'd"
        runner._db.execute.assert_not_awaited()

    def test_task_delegated_nonpositive_saving_is_truthful_empty(self) -> None:
        """cost_usd >= counterfactual_cost_usd → no saving → no row, no DLQ."""
        published, capture = _capture()
        runner = SavingsProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        # cost_usd=1.0 far exceeds the counterfactual (~0.0525); saving is negative.
        data = _task_delegated_payload(cost_usd="1.0")
        meta = MessageMeta(partition=0, offset=2, fallback_id=CORR_ID)

        ok = asyncio.run(runner.project_event(TASK_DELEGATED_TOPIC, data, meta))

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == SAVINGS_DLQ_TOPIC]
        assert dlq_rows == [], "non-positive saving must NOT be DLQ'd"
        runner._db.execute.assert_not_awaited()

    def test_task_delegated_savings_amounts_are_correct(self) -> None:
        """Savings = counterfactual_cost_usd - measured actual cost_usd."""
        published, capture = _capture()
        runner = SavingsProjectionRunner(publish_fn=capture)

        # Intercept the _upsert_savings_estimate call to inspect arguments.
        captured_kwargs: list[dict[str, Any]] = []

        async def capture_upsert(**kwargs: Any) -> None:
            captured_kwargs.append(kwargs)

        runner._upsert_savings_estimate = capture_upsert  # type: ignore[method-assign]

        data = _task_delegated_payload(cost_usd="0.003")
        meta = MessageMeta(partition=0, offset=3, fallback_id=CORR_ID)

        ok = asyncio.run(runner.project_event(TASK_DELEGATED_TOPIC, data, meta))

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == SAVINGS_DLQ_TOPIC]
        assert dlq_rows == []
        assert len(captured_kwargs) == 1
        kwargs = captured_kwargs[0]
        assert kwargs["local_cost_usd"] == Decimal("0.003")
        assert kwargs["cloud_cost_usd"] == Decimal("0.0525")
        assert kwargs["savings_usd"] == Decimal("0.0495")
        assert kwargs["model_local"] == "glm-5.2"

    def test_savings_runner_contract_binding_matches_deployed_catalog(self) -> None:
        """The deployed catalog command points at handler_savings.SavingsProjectionRunner.
        Confirm that handler is what we just fixed (not the other handler)."""
        import importlib

        module = importlib.import_module(
            "omnimarket.nodes.node_projection_savings.handlers.handler_savings"
        )
        assert hasattr(module, "SavingsProjectionRunner"), (
            "handler_savings must export SavingsProjectionRunner "
            "(the deployed catalog binding)"
        )
        # The deployed runner must now expose _topic_delegated to handle
        # task-delegated.v1 events.
        runner = module.SavingsProjectionRunner()
        assert hasattr(runner, "_topic_delegated"), (
            "SavingsProjectionRunner must declare _topic_delegated "
            "after the OMN-13598 fix"
        )
        assert runner._topic_delegated == TASK_DELEGATED_TOPIC
