"""Tests for projection handler event extraction logic.

These tests verify field extraction and SQL parameter construction
without connecting to a real database. They mock the AsyncpgAdapter
and verify the handler calls execute() with the correct arguments.
"""

from __future__ import annotations

from decimal import Decimal
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
        # Should default to unknown for unrecognized source
        call_args = mock_db.execute.call_args[0]
        # call_args[0] is SQL; usage_source is the $8 bind value -> index 8.
        assert call_args[8] == "unknown"


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

    @pytest.mark.asyncio
    async def test_generation_completed_projected(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        runner = DelegationProjectionRunner()
        runner._db = mock_db

        data = {
            "correlation_id": "gen-corr-1",
            "task_description": "Build a node that validates email addresses",
            "provider": "local",
            "model_id": "Qwen3-Coder-30B",
            "endpoint_class": "local",
            "attempt_count": 1,
            "total_latency_e2e_ms": 3200,
            "contract_passed": True,
            "cost_inference_usd": 0.0,
        }

        result = await runner.project_event(
            "onex.evt.omnimarket.node-generation-completed.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()
        args = mock_db.execute.call_args[0]
        assert "gen-corr-1" in args
        assert "Build a node that validates email addresses" in args

    @pytest.mark.asyncio
    async def test_generation_completed_contract_failed(
        self, mock_db: AsyncMock
    ) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        runner = DelegationProjectionRunner()
        runner._db = mock_db

        data = {
            "correlation_id": "gen-corr-fail",
            "task_description": "Generate a broken node",
            "contract_passed": False,
            "attempt_count": 2,
        }

        result = await runner.project_event(
            "onex.evt.omnimarket.node-generation-completed.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()
        args = mock_db.execute.call_args[0]
        assert any(a is False for a in args)  # contract_passed=False projected

    @pytest.mark.asyncio
    async def test_unknown_topic_returns_false(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        runner = DelegationProjectionRunner()
        runner._db = mock_db

        result = await runner.project_event(
            "onex.evt.omnimarket.some-unknown-topic.v1", {}, _make_meta()
        )
        assert result is False
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_generation_fallback_correlation_id(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        runner = DelegationProjectionRunner()
        runner._db = mock_db

        meta = _make_meta()
        data = {"task_description": "no correlation id here"}  # no correlation_id

        result = await runner.project_event(
            "onex.evt.omnimarket.node-generation-completed.v1", data, meta
        )
        assert result is True
        mock_db.execute.assert_called_once()
        args = mock_db.execute.call_args[0]
        assert meta.fallback_id in args

    @pytest.mark.asyncio
    async def test_delegate_skill_terminal_uses_typed_projection_model(
        self, mock_db: AsyncMock
    ) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        runner = DelegationProjectionRunner()
        runner._db = mock_db

        data = {
            "status": "completed",
            "correlation_id": "4ae8556b-af7c-4e85-a7f5-9388d60cebb5",
            "session_id": "19ee51d6-d275-4642-8cb5-19cdce2af447",
            "task_type": "test",
            "provider": "local-qwen",
            "model_name": "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit",
            "response": "projection proof",
            "quality_gate_passed": True,
            "quality_gates_failed": [],
            "metrics": {
                "input_tokens": 144,
                "output_tokens": 593,
                "total_tokens": 737,
                "tokens_to_compliance": 737,
                "compliance_attempts": 1,
                "cost_usd": 0.0,
                "cost_savings_usd": 0.009327,
                "latency_ms": 1250,
            },
            "_envelope": {
                "envelope_timestamp": "2026-05-20T17:03:00Z",
            },
        }

        result = await runner.project_event(
            "onex.evt.omnimarket.delegate-skill-completed.v1", data, _make_meta()
        )

        assert result is True
        mock_db.execute.assert_called_once()
        args = mock_db.execute.call_args[0]
        assert "tokens_input" in args[0]
        assert "ON CONFLICT (correlation_id) DO UPDATE SET" in args[0]
        assert "4ae8556b-af7c-4e85-a7f5-9388d60cebb5" in args
        assert "19ee51d6-d275-4642-8cb5-19cdce2af447" in args
        assert "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit" in args
        assert 144 in args
        assert 593 in args
        assert 737 in args


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
            "event_timestamp": "2026-04-06T12:00:00Z",
            "model_local": "qwen3-coder-30b",
            "model_cloud_baseline": "claude-opus-4",
            "local_cost_usd": "0.010000",
            "cloud_cost_usd": "0.050000",
            "savings_usd": "0.040000",
        }

        result = await runner.project_event(
            "onex.evt.omnibase-infra.savings-estimated.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args[0]
        assert "updated_at = NOW()" in call_args[0]
        assert call_args[1].isoformat() == "2026-04-06T12:00:00+00:00"
        assert call_args[5] == Decimal("0.010000")
        assert call_args[6] == Decimal("0.050000")
        assert call_args[7] == Decimal("0.040000")

    @pytest.mark.asyncio
    async def test_missing_session_id_skips(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
            SavingsProjectionRunner,
        )

        runner = SavingsProjectionRunner()
        runner._db = mock_db

        data = {"model_local": "qwen3-coder-30b"}
        result = await runner.project_event(
            "onex.evt.omnibase-infra.savings-estimated.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_required_amount_skips(self, mock_db: AsyncMock) -> None:
        from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
            SavingsProjectionRunner,
        )

        runner = SavingsProjectionRunner()
        runner._db = mock_db

        data = {
            "session_id": "sess-savings-invalid",
            "event_timestamp": "2026-04-06T12:00:00Z",
            "model_local": "qwen3-coder-30b",
            "model_cloud_baseline": "claude-opus-4",
            "local_cost_usd": "not-a-decimal",
            "cloud_cost_usd": "0.050000",
            "savings_usd": "0.040000",
        }
        result = await runner.project_event(
            "onex.evt.omnibase-infra.savings-estimated.v1", data, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_delegate_skill_terminal_projects_typed_savings(
        self, mock_db: AsyncMock
    ) -> None:
        from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
            SavingsProjectionRunner,
        )

        runner = SavingsProjectionRunner()
        runner._db = mock_db

        data = {
            "status": "completed",
            "correlation_id": "f9243395-5cb6-4036-8ffb-39dd25547413",
            "task_type": "document",
            "provider": "local-qwen",
            "model_name": "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit",
            "quality_gate_passed": True,
            "metrics": {
                "input_tokens": 81,
                "output_tokens": 384,
                "total_tokens": 465,
                "cost_usd": 0.0,
                "cost_savings_usd": 0.006003,
                "latency_ms": 900,
            },
            "_envelope": {
                "envelope_timestamp": "2026-05-20T17:05:00Z",
            },
        }

        result = await runner.project_event(
            "onex.evt.omnimarket.delegate-skill-completed.v1", data, _make_meta()
        )

        assert result is True
        mock_db.execute.assert_called_once()
        args = mock_db.execute.call_args[0]
        assert args[1].isoformat() == "2026-05-20T17:05:00+00:00"
        assert args[2] == "f9243395-5cb6-4036-8ffb-39dd25547413"
        assert args[3] == "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit"
        assert args[4] == "claude-sonnet-4-6"
        assert args[5] == Decimal("0.0")
        assert args[6] == Decimal("0.006003")
        assert args[7] == Decimal("0.006003")
