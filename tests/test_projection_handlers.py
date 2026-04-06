"""Tests for projection handler event extraction logic.

These tests verify field extraction and SQL parameter construction
without connecting to a real database. They mock the AsyncpgAdapter
and verify the handler calls execute() with the correct arguments.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from omnimarket.projection.runner import MessageMeta


def _make_meta(partition: int = 0, offset: int = 0) -> MessageMeta:
    return MessageMeta(
        partition=partition, offset=offset, fallback_id="fallback-id-1234"
    )


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.execute_many = AsyncMock()
    db.execute_in_transaction = AsyncMock()
    db.fetchval = AsyncMock(return_value=None)
    db.connect = AsyncMock()
    db.close = AsyncMock()
    return db


class TestSessionOutcomeHandler:
    @pytest.mark.asyncio
    async def test_basic_projection(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_session_outcome.handlers.handler_session_outcome import (
            SessionOutcomeProjectionRunner,
        )

        runner = SessionOutcomeProjectionRunner()
        runner._db = mock_db

        data = {
            "session_id": "sess-001",
            "outcome": "success",
            "emitted_at": "2026-04-06T12:00:00Z",
        }

        result = await runner.project_event(
            "onex.evt.omniclaude.session-outcome.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()
        args = mock_db.execute.call_args
        assert "sess-001" in args[0]
        assert "success" in args[0]

    @pytest.mark.asyncio
    async def test_missing_session_id_skips(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_session_outcome.handlers.handler_session_outcome import (
            SessionOutcomeProjectionRunner,
        )

        runner = SessionOutcomeProjectionRunner()
        runner._db = mock_db

        data = {"outcome": "success"}
        result = await runner.project_event(
            "onex.evt.omniclaude.session-outcome.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_correlation_id_fallback(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_session_outcome.handlers.handler_session_outcome import (
            SessionOutcomeProjectionRunner,
        )

        runner = SessionOutcomeProjectionRunner()
        runner._db = mock_db

        data = {"correlation_id": "corr-123", "outcome": "failure"}
        result = await runner.project_event(
            "onex.evt.omniclaude.session-outcome.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()
        args = mock_db.execute.call_args
        assert "corr-123" in args[0]


class TestLlmCostHandler:
    @pytest.mark.asyncio
    async def test_basic_projection(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_llm_cost.handlers.handler_llm_cost import (
            LlmCostProjectionRunner,
        )

        runner = LlmCostProjectionRunner()
        runner._db = mock_db

        data = {
            "model_id": "claude-sonnet-4-6",
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "total_tokens": 1500,
            "estimated_cost_usd": 0.015,
            "timestamp": "2026-04-06T12:00:00Z",
        }

        result = await runner.project_event(
            "onex.evt.omniintelligence.llm-call-completed.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_usage_source_normalization(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_llm_cost.handlers.handler_llm_cost import (
            LlmCostProjectionRunner,
        )

        runner = LlmCostProjectionRunner()
        runner._db = mock_db

        data = {
            "model_id": "test-model",
            "usage_source": "invalid_source",
            "timestamp": "2026-04-06T12:00:00Z",
        }

        result = await runner.project_event(
            "onex.evt.omniintelligence.llm-call-completed.v1", data, _make_meta()
        )
        assert result is True
        # Should default to API for unrecognized source
        call_args = mock_db.execute.call_args[0]
        # The 8th positional param is usage_source (index 7 in the args tuple)
        assert "API" in call_args


class TestDelegationHandler:
    @pytest.mark.asyncio
    async def test_task_delegated(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        runner = DelegationProjectionRunner()
        runner._db = mock_db

        data = {
            "correlation_id": "corr-del-1",
            "task_type": "code_review",
            "delegated_to": "claude-haiku-4-5",
            "timestamp": "2026-04-06T12:00:00Z",
        }

        result = await runner.project_event(
            "onex.evt.omniclaude.task-delegated.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_shadow_comparison(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        runner = DelegationProjectionRunner()
        runner._db = mock_db

        data = {
            "correlation_id": "corr-shadow-1",
            "task_type": "code_review",
            "primary_agent": "claude-sonnet-4-6",
            "shadow_agent": "claude-haiku-4-5",
            "divergence_detected": True,
        }

        result = await runner.project_event(
            "onex.evt.omniclaude.delegation-shadow-comparison.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_required_fields_skips(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        runner = DelegationProjectionRunner()
        runner._db = mock_db

        data = {"correlation_id": "corr-1"}  # missing task_type, delegated_to
        result = await runner.project_event(
            "onex.evt.omniclaude.task-delegated.v1", data, _make_meta()
        )
        assert result is True  # skip, don't error
        mock_db.execute.assert_not_called()


class TestRegistrationHandler:
    @pytest.mark.asyncio
    async def test_introspection(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_registration.handlers.handler_registration import (
            RegistrationProjectionRunner,
        )

        runner = RegistrationProjectionRunner()
        runner._db = mock_db

        data = {
            "node_name": "node_build_loop",
            "node_id": "abc-123",
            "service_url": "http://localhost:8080",
            "health_status": "healthy",
            "metadata": {"version": "1.0"},
        }

        result = await runner.project_event(
            "onex.evt.platform.node-introspection.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_heartbeat(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_registration.handlers.handler_registration import (
            RegistrationProjectionRunner,
        )

        runner = RegistrationProjectionRunner()
        runner._db = mock_db

        data = {"node_name": "node_build_loop", "health_status": "healthy"}

        result = await runner.project_event(
            "onex.evt.platform.node-heartbeat.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_state_change(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_registration.handlers.handler_registration import (
            RegistrationProjectionRunner,
        )

        runner = RegistrationProjectionRunner()
        runner._db = mock_db

        data = {"node_name": "node_build_loop", "new_state": "active"}

        result = await runner.project_event(
            "onex.evt.platform.node-state-change.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()


class TestBaselinesHandler:
    @pytest.mark.asyncio
    async def test_basic_projection(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_baselines.handlers.handler_baselines import (
            BaselinesProjectionRunner,
        )

        runner = BaselinesProjectionRunner()
        runner._db = mock_db

        data = {
            "snapshot_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "contract_version": 2,
            "computed_at_utc": "2026-04-06T12:00:00Z",
            "comparisons": [
                {
                    "pattern_id": "p1",
                    "pattern_name": "test_pattern",
                    "sample_size": 100,
                    "window_start": "2026-04-01",
                    "window_end": "2026-04-06",
                    "recommendation": "promote",
                    "confidence": "high",
                }
            ],
            "trend": [
                {
                    "date": "2026-04-05",
                    "avg_cost_savings": 0.15,
                    "avg_outcome_improvement": 0.2,
                }
            ],
            "breakdown": [{"action": "promote", "count": 5, "avg_confidence": 0.8}],
        }

        result = await runner.project_event(
            "onex.evt.omnibase-infra.baselines-computed.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute_in_transaction.assert_called_once()

        # Verify the transaction has the right number of queries:
        # 1 snapshot upsert + 1 delete comparisons + 1 insert comparison
        # + 1 delete trend + 1 insert trend + 1 delete breakdown + 1 insert breakdown = 7
        queries = mock_db.execute_in_transaction.call_args[0][0]
        assert len(queries) == 7


class TestSavingsHandler:
    @pytest.mark.asyncio
    async def test_basic_projection(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
            SavingsProjectionRunner,
        )

        runner = SavingsProjectionRunner()
        runner._db = mock_db

        data = {
            "session_id": "sess-savings-1",
            "correlation_id": "corr-sav-1",
            "actual_total_tokens": 5000,
            "actual_cost_usd": 0.05,
            "direct_savings_usd": 0.03,
            "estimated_total_savings_usd": 0.04,
            "timestamp": "2026-04-06T12:00:00Z",
        }

        result = await runner.project_event(
            "onex.evt.omnibase-infra.savings-estimated.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_session_id_skips(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
            SavingsProjectionRunner,
        )

        runner = SavingsProjectionRunner()
        runner._db = mock_db

        data = {"actual_total_tokens": 100}
        result = await runner.project_event(
            "onex.evt.omnibase-infra.savings-estimated.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_not_called()
