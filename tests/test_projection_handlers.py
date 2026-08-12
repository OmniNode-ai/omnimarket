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

_DELEGATE_SKILL_TEST_MODEL = "test-model-local"


def _make_meta(partition: int = 0, offset: int = 0) -> MessageMeta:
    return MessageMeta(
        partition=partition, offset=offset, fallback_id="fallback-id-1234"
    )


def _param_by_column(call_args: tuple[object, ...]) -> dict[str, object]:
    """Map column name -> bound value for a dynamic-UPSERT INSERT call.

    OMN-15905: the ported ``DelegationProjectionRunner._dynamic_upsert``
    builds its column list (and therefore its positional-param order) from a
    Python dict's insertion order, which shifts whenever a new column is
    ported in. Tests that need to assert a specific column's bound value use
    this name-based lookup instead of a fragile ``args[-N]`` index.
    """
    sql = str(call_args[0])
    columns_segment = sql.split("(", 1)[1].split(")", 1)[0]
    columns = [c.strip() for c in columns_segment.split(",")]
    values = call_args[1:]
    return dict(zip(columns, values, strict=True))


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
        # OMN-13001: writes llm_call_metrics. An unrecognized usage_source maps
        # to the DB enum's MISSING value. call_args[0] is SQL; the 10th INSERT
        # column (usage_source) is the 10th positional bind -> index 10.
        call_args = mock_db.execute.call_args[0]
        assert call_args[10] == "MISSING"


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
        # OMN-15905: the ported evidence-preservation step issues a SELECT
        # before the write (parity with the sync HandlerProjectionDelegation
        # path) -- 1 SELECT + 1 INSERT.
        assert mock_db.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_task_delegated_preserves_manifest_pricing_fields(
        self, mock_db: AsyncMock
    ) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        runner = DelegationProjectionRunner()
        runner._db = mock_db

        data = {
            "correlation_id": "corr-pricing-proof",
            "task_type": "test",
            "delegated_to": "Qwen3-Coder-30B-A3B",
            "timestamp": "2026-05-21T12:00:00Z",
            "cost_usd": 0.0,
            "cost_savings_usd": 0.00525,
            "pricing_manifest_version": 1,
        }

        result = await runner.project_event(
            "onex.evt.omniclaude.task-delegated.v1", data, _make_meta()
        )

        assert result is True
        # OMN-15905: SELECT (evidence preservation) + INSERT.
        assert mock_db.execute.call_count == 2
        args = mock_db.execute.call_args[0]
        assert "pricing_manifest_version" in args[0]
        assert "corr-pricing-proof" in args
        # OMN-15905: the ported write path stores the measured actual cost as
        # a native float/Decimal param (matching the sync project() path),
        # not a pre-stringified value the old async-only converter produced.
        by_column = _param_by_column(args)
        assert by_column["cost_savings_usd"] == 0.00525
        assert by_column["pricing_manifest_version"] == 1

    @pytest.mark.asyncio
    async def test_delegation_terminal_enriches_nested_result_payload(
        self, mock_db: AsyncMock
    ) -> None:
        """OMN-15905: ``data`` here is the FLAT canonical terminal payload --
        the shape ``project_event`` actually receives in production, since
        ``BaseProjectionRunner._handle_message`` always runs the raw Kafka
        message through ``unwrap_envelope()`` first (unwrapping a
        ``{"payload": {...}}`` broker envelope into its flat inner dict)
        before ever calling ``project_event``. The pre-port async-runner-only
        converter defensively re-unwrapped a nested ``{"topic":...,
        "payload": {...}}`` shape that ``project_event`` never actually
        receives; the ported (correct) converter matches the sync
        ``HandlerProjectionDelegation.handle()`` contract, which has never
        needed that second unwrap either.
        """
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        runner = DelegationProjectionRunner()
        runner._db = mock_db

        useful_response = (
            "import pytest\n\n"
            "@pytest.mark.unit\n"
            "def test_projection_terminal_payload():\n"
            "    assert True\n"
        )
        data = {
            "correlation_id": "c65f5188-4250-4b42-8a45-b9e355b207ee",
            "task_type": "test_generation",
            "model_used": "Qwen3.6-27B-MTP-IQ4_XS.gguf",
            "prompt_text": "Write focused pytest coverage for the projection reducer.",
            "content": useful_response,
            "quality_passed": True,
            "quality_score": 1.0,
            "latency_ms": 2400,
            "prompt_tokens": 321,
            "completion_tokens": 654,
        }

        result = await runner.project_event(
            "onex.evt.omnibase-infra.delegation-completed.v1", data, _make_meta()
        )

        assert result is True
        # OMN-15905: SELECT (evidence preservation) + INSERT.
        assert mock_db.execute.call_count == 2
        args = mock_db.execute.call_args[0]
        assert "ON CONFLICT (correlation_id) DO UPDATE SET" in args[0]
        assert "c65f5188-4250-4b42-8a45-b9e355b207ee" in args
        assert "Qwen3.6-27B-MTP-IQ4_XS.gguf" in args
        assert "Write focused pytest coverage for the projection reducer." in args
        assert useful_response in args
        assert 321 in args
        assert 654 in args

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
            "model_name": _DELEGATE_SKILL_TEST_MODEL,
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
            "pricing_manifest_version": 1,
            "_envelope": {
                "envelope_timestamp": "2026-05-20T17:03:00Z",
            },
        }

        result = await runner.project_event(
            "onex.evt.omnimarket.delegate-skill-completed.v1", data, _make_meta()
        )

        assert result is True
        # OMN-15905: SELECT (evidence preservation) + INSERT.
        assert mock_db.execute.call_count == 2
        args = mock_db.execute.call_args[0]
        assert "tokens_input" in args[0]
        assert "quality_gates_checked_jsonb" in args[0]
        assert "quality_gates_failed_jsonb" in args[0]
        assert "ON CONFLICT (correlation_id) DO UPDATE SET" in args[0]
        assert "4ae8556b-af7c-4e85-a7f5-9388d60cebb5" in args
        assert "19ee51d6-d275-4642-8cb5-19cdce2af447" in args
        assert _DELEGATE_SKILL_TEST_MODEL in args
        assert 1 in args
        assert "[]" in args
        assert 144 in args
        assert 593 in args
        assert 737 in args
        # OMN-12606: the materializer provenance columns are stamped.
        assert "projection_version" in args[0]
        assert "reducer_version" in args[0]
        by_column = _param_by_column(args)
        assert by_column["pricing_manifest_version"] == 1
        assert by_column["projection_version"] == "1.0.0"
        assert by_column["reducer_version"] == "1.0.0"


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
    async def test_introspection_rich_fields_survive_metadata_jsonb(
        self, mock_db: AsyncMock
    ) -> None:
        """OMN-14490: the async runner (the live Kafka->Postgres projector) must
        NOT silently drop the producer's rich introspection fields on the very
        hot node-introspection.v1 topic. Drive the producer's ACTUAL canonical
        wire payload and assert endpoints / declared_capabilities /
        discovered_capabilities / contract_capabilities / current_state all
        survive into the metadata JSONB bind.

        RED against exists-but-wrong: the prior runner persisted only
        node_name/node_id into metadata, so these keys were absent (KeyError).
        """
        import json
        from datetime import UTC, datetime
        from uuid import uuid4

        from omnibase_core.enums.enum_node_kind import EnumNodeKind
        from omnibase_core.models.primitives.model_semver import ModelSemVer
        from omnibase_infra.models.registration.model_node_introspection_event import (
            ModelContractCapabilities,
            ModelDiscoveredCapabilities,
            ModelNodeCapabilities,
            ModelNodeIntrospectionEvent,
        )

        from omnimarket.nodes.node_projection_registration.handlers.handler_registration import (
            RegistrationProjectionRunner,
        )

        runner = RegistrationProjectionRunner()
        runner._db = mock_db

        event = ModelNodeIntrospectionEvent(
            node_id=uuid4(),
            node_name="rich-runner-svc",
            node_type=EnumNodeKind.COMPUTE,
            correlation_id=uuid4(),
            timestamp=datetime.now(tz=UTC),
            endpoints={"http": "http://rich-runner:8080"},
            declared_capabilities=ModelNodeCapabilities(),
            discovered_capabilities=ModelDiscoveredCapabilities(has_fsm=True),
            contract_capabilities=ModelContractCapabilities(
                contract_type="COMPUTE_GENERIC",
                contract_version=ModelSemVer(major=2, minor=0, patch=0),
                capability_tags=["omn14490-runner"],
            ),
            current_state="RUNNING",
        )
        # Drive the EXACT producer wire payload through the async projector.
        wire = event.model_dump(mode="json")
        result = await runner.project_event(
            "onex.evt.platform.node-introspection.v1", wire, _make_meta()
        )
        assert result is True
        mock_db.execute.assert_called_once()

        # Positional binds: (sql, service_name, service_url, service_type,
        # health_status, metadata_json) -> metadata_json is index 5.
        metadata = json.loads(mock_db.execute.call_args[0][5])
        assert metadata["endpoints"] == {"http": "http://rich-runner:8080"}
        assert metadata["declared_capabilities"] == wire["declared_capabilities"]
        assert metadata["discovered_capabilities"]["has_fsm"] is True
        assert metadata["contract_capabilities"]["capability_tags"] == [
            "omn14490-runner"
        ]
        assert metadata["current_state"] == "RUNNING"

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
        """OMN-14513: drive a real producer-shaped ModelBaselinesSnapshotEvent.

        Previously this test hand-built a fictional payload
        (comparisons.pattern_id/recommendation, trend.date/avg_cost_savings,
        breakdown.action/count) that the real producer never sends. Building
        the payload from the producer's own model_dump() proves the fix
        parses what the wire contract actually carries.
        """
        from datetime import UTC, date, datetime
        from uuid import uuid4

        from omnibase_infra.services.observability.baselines.models.model_baselines_breakdown_row import (
            ModelBaselinesBreakdownRow,
        )
        from omnibase_infra.services.observability.baselines.models.model_baselines_comparison_row import (
            ModelBaselinesComparisonRow,
        )
        from omnibase_infra.services.observability.baselines.models.model_baselines_snapshot_event import (
            ModelBaselinesSnapshotEvent,
        )
        from omnibase_infra.services.observability.baselines.models.model_baselines_trend_row import (
            ModelBaselinesTrendRow,
        )

        from omnimarket.nodes.node_projection_baselines.handlers.handler_baselines import (
            BaselinesProjectionRunner,
        )

        runner = BaselinesProjectionRunner()
        runner._db = mock_db

        now = datetime(2026, 4, 6, 12, 0, 0, tzinfo=UTC)
        producer_event = ModelBaselinesSnapshotEvent(
            snapshot_id=uuid4(),
            contract_version=2,
            computed_at_utc=now,
            comparisons=[
                ModelBaselinesComparisonRow(
                    id=uuid4(),
                    comparison_date=date(2026, 4, 6),
                    treatment_sessions=100,
                    control_sessions=90,
                    roi_pct=12.5,
                    sample_size=190,
                    computed_at=now,
                    created_at=now,
                    updated_at=now,
                )
            ],
            trend=[
                ModelBaselinesTrendRow(
                    id=uuid4(),
                    trend_date=date(2026, 4, 5),
                    cohort="treatment",
                    session_count=50,
                    success_rate=0.9,
                    computed_at=now,
                    created_at=now,
                )
            ],
            breakdown=[
                ModelBaselinesBreakdownRow(
                    id=uuid4(),
                    pattern_id=uuid4(),
                    pattern_label="retry-guard",
                    treatment_success_rate=0.92,
                    sample_count=100,
                    computed_at=now,
                    created_at=now,
                    updated_at=now,
                )
            ],
        )
        data = producer_event.model_dump(mode="json")

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
            "model_name": _DELEGATE_SKILL_TEST_MODEL,
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
        assert args[3] == _DELEGATE_SKILL_TEST_MODEL
        assert args[4] == "claude-opus-4-6"
        assert args[5] == Decimal("0.0")
        assert args[6] == Decimal("0.006003")
        assert args[7] == Decimal("0.006003")
