# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13598 / OMN-13629: deployed SavingsProjectionRunner materializes
savings_estimates from the canonical delegation terminal pair.

History: OMN-13598 wired the SavingsProjectionRunner to the legacy compat
task-delegated.v1 SOURCE stream. OMN-13629 (WS-F Phase 1) collapsed the terminal
to a single canonical event and REPOINTED the savings runner onto
delegation-{completed,failed}.v1 — these tests now drive the canonical shape. The
deployed catalog binding
(omnibase_infra/docker/catalog/services/omnimarket-projection-savings.yaml) points
at handler_savings.SavingsProjectionRunner, so the repoint lands there.
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
from omnimarket.projection.runner import MessageMeta

DELEGATION_COMPLETED_TOPIC = "onex.evt.omnibase-infra.delegation-completed.v1"
DELEGATION_FAILED_TOPIC = "onex.evt.omnibase-infra.delegation-failed.v1"
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


def _canonical_completed_payload(
    *,
    cumulative_cost: float = 0.003,
    prompt_tokens: int = 1000,
    completion_tokens: int = 500,
    quality_passed: bool = True,
) -> dict[str, Any]:
    """Canonical ModelDelegationResult terminal payload (delegation-completed.v1)."""
    return {
        "correlation_id": CORR_ID,
        "task_type": "code_generation",
        "model_used": "glm-5.2",
        "quality_passed": quality_passed,
        "cumulative_attempt_cost": cumulative_cost,
        "final_attempt_cost": cumulative_cost,
        "cumulative_input_tokens": prompt_tokens,
        "cumulative_output_tokens": completion_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "timestamp": "2026-06-20T12:00:00+00:00",
    }


@pytest.mark.unit
class TestSavingsRunnerCanonicalTerminal:
    """SavingsProjectionRunner (deployed handler) processes the canonical pair."""

    def test_canonical_topics_in_subscribe_topics(self) -> None:
        """Contract must declare the canonical delegation pair as subscribed."""
        runner = SavingsProjectionRunner()
        assert DELEGATION_COMPLETED_TOPIC in runner.subscribe_topics
        assert DELEGATION_FAILED_TOPIC in runner.subscribe_topics

    def test_legacy_task_delegated_topic_not_subscribed(self) -> None:
        runner = SavingsProjectionRunner()
        assert (
            "onex.evt.omniclaude.task-delegated.v1"  # onex-topic-allow: negative proof
            not in runner.subscribe_topics
        )

    def test_completed_with_derivable_counterfactual_upserts_row(self) -> None:
        """A canonical completed terminal whose re-derived counterfactual beats the
        measured cost must upsert a row, not be DLQ'd as malformed."""
        published, capture = _capture()
        runner = SavingsProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        data = _canonical_completed_payload()
        meta = MessageMeta(partition=0, offset=0, fallback_id=CORR_ID)

        ok = asyncio.run(runner.project_event(DELEGATION_COMPLETED_TOPIC, data, meta))

        assert ok is True, "project_event must return True (offset committed)"
        dlq_rows = [(t, v) for t, v in published if t == SAVINGS_DLQ_TOPIC]
        assert dlq_rows == [], (
            f"canonical completed terminal must NOT be DLQ'd; "
            f"got DLQ entries: {[json.loads(v) for _, v in dlq_rows]}"
        )
        runner._db.execute.assert_awaited_once()

    def test_failed_terminal_is_truthful_empty(self) -> None:
        """A FAILED terminal banks no saving (no counterfactual) -> no row, no DLQ."""
        published, capture = _capture()
        runner = SavingsProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        data = _canonical_completed_payload(quality_passed=False)
        meta = MessageMeta(partition=0, offset=1, fallback_id=CORR_ID)

        ok = asyncio.run(runner.project_event(DELEGATION_FAILED_TOPIC, data, meta))

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == SAVINGS_DLQ_TOPIC]
        assert dlq_rows == [], "failed terminal must NOT be DLQ'd"
        runner._db.execute.assert_not_awaited()

    def test_nonpositive_saving_is_truthful_empty(self) -> None:
        """measured cost >= re-derived counterfactual -> no saving -> no row, no DLQ."""
        published, capture = _capture()
        runner = SavingsProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        # A large measured cost far exceeds the re-derived counterfactual.
        data = _canonical_completed_payload(cumulative_cost=1.0)
        meta = MessageMeta(partition=0, offset=2, fallback_id=CORR_ID)

        ok = asyncio.run(runner.project_event(DELEGATION_COMPLETED_TOPIC, data, meta))

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == SAVINGS_DLQ_TOPIC]
        assert dlq_rows == [], "non-positive saving must NOT be DLQ'd"
        runner._db.execute.assert_not_awaited()

    def test_savings_amounts_are_correct(self) -> None:
        """Savings = re-derived counterfactual_cost_usd - measured cumulative cost."""
        from omnimarket.pricing import build_premium_counterfactual

        published, capture = _capture()
        runner = SavingsProjectionRunner(publish_fn=capture)

        captured_kwargs: list[dict[str, Any]] = []

        async def capture_upsert(**kwargs: Any) -> None:
            captured_kwargs.append(kwargs)

        runner._upsert_savings_estimate = capture_upsert  # type: ignore[method-assign]

        data = _canonical_completed_payload(cumulative_cost=0.003)
        meta = MessageMeta(partition=0, offset=3, fallback_id=CORR_ID)

        ok = asyncio.run(runner.project_event(DELEGATION_COMPLETED_TOPIC, data, meta))

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == SAVINGS_DLQ_TOPIC]
        assert dlq_rows == []
        assert len(captured_kwargs) == 1
        kwargs = captured_kwargs[0]
        # The cloud baseline is re-derived from the served tokens.
        cf = build_premium_counterfactual(prompt_tokens=1000, completion_tokens=500)
        assert cf is not None
        assert kwargs["local_cost_usd"] == Decimal("0.003")
        assert kwargs["cloud_cost_usd"] == cf.counterfactual_cost_usd
        assert kwargs["savings_usd"] == cf.counterfactual_cost_usd - Decimal("0.003")
        assert kwargs["model_local"] == "glm-5.2"

    def test_savings_runner_contract_binding_matches_deployed_catalog(self) -> None:
        """The deployed catalog command points at handler_savings.SavingsProjectionRunner.
        Confirm the repointed canonical topic fields are present."""
        import importlib

        module = importlib.import_module(
            "omnimarket.nodes.node_projection_savings.handlers.handler_savings"
        )
        assert hasattr(module, "SavingsProjectionRunner")
        runner = module.SavingsProjectionRunner()
        # OMN-13629: the runner now resolves the canonical delegation pair.
        assert runner._topic_delegation_completed == DELEGATION_COMPLETED_TOPIC
        assert runner._topic_delegation_failed == DELEGATION_FAILED_TOPIC
