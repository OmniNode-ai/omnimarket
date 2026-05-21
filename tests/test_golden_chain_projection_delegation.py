"""Golden chain tests for node_projection_delegation."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionDelegation()
_DELEGATE_SKILL_TEST_MODEL = "test-model-local"


class TestDelegationProjection:
    def test_project_single_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-001",
            task_type="code-review",
            delegated_to="agent-alpha",
            delegated_by="team-lead",
            quality_gate_passed=True,
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("delegation_events")
        assert len(rows) == 1
        assert rows[0]["task_type"] == "code-review"
        assert rows[0]["quality_gate_passed"] is True

    def test_dedup_by_correlation_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelTaskDelegatedEvent(
                correlation_id="corr-001",
                task_type="refactor",
                delegated_to="agent-a",
            ),
            db,
        )
        HANDLER.project(
            ModelTaskDelegatedEvent(
                correlation_id="corr-001",
                task_type="test-generation",
                delegated_to="agent-b",
            ),
            db,
        )
        rows = db.query("delegation_events")
        assert len(rows) == 1
        # Second write wins (UPSERT)
        assert rows[0]["task_type"] == "test-generation"

    def test_project_batch(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [
            ModelTaskDelegatedEvent(
                correlation_id=f"corr-{i:03d}",
                task_type="code-review",
                delegated_to=f"agent-{i}",
            )
            for i in range(3)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 3

    def test_llm_call_id_projected(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-llm",
            task_type="code-review",
            delegated_to="agent-alpha",
            llm_call_id="chatcmpl-abc123",
        )
        HANDLER.project(event, db)
        rows = db.query("delegation_events")
        assert len(rows) == 1
        assert rows[0]["llm_call_id"] == "chatcmpl-abc123"

    def test_llm_call_id_defaults_empty(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-no-llm",
            task_type="code-review",
            delegated_to="agent-alpha",
        )
        HANDLER.project(event, db)
        rows = db.query("delegation_events")
        assert len(rows) == 1
        assert rows[0]["llm_call_id"] is None

    def test_shadow_delegation(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-shadow",
            task_type="code-review",
            delegated_to="shadow-agent",
            is_shadow=True,
        )
        HANDLER.project(event, db)
        rows = db.query("delegation_events", {"is_shadow": True})
        assert len(rows) == 1

    def test_event_bus_wiring(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            contract["handler"]["module"]
            == "omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation"
        )
        assert contract["handler"]["class"] == "HandlerProjectionDelegation"
        topics = contract["event_bus"]["subscribe_topics"]
        assert "onex.evt.omniclaude.task-delegated.v1" in topics
        assert "onex.evt.omnimarket.node-generation-completed.v1" in topics
        assert "onex.evt.omnimarket.delegate-skill-completed.v1" in topics
        assert "onex.evt.omnimarket.delegate-skill-failed.v1" in topics

    def test_delegate_skill_metrics_migration_declares_dashboard_columns(self) -> None:
        migration = Path(
            "src/omnimarket/nodes/node_projection_delegation/migrations/"
            "0009_delegate_skill_projection_metrics.sql"
        ).read_text()
        assert "tokens_input INT NOT NULL DEFAULT 0" in migration
        assert "tokens_output INT NOT NULL DEFAULT 0" in migration
        assert "quality_gate_detail TEXT" in migration
        assert "latency_ms INT" in migration
        assert "pricing_manifest_version INT NOT NULL DEFAULT 0" in migration

    def test_sync_handler_projects_delegate_skill_terminal_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "_db": db,
            "_event_type": "delegate-skill-completed",
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
        }

        result = HANDLER.handle(payload)

        assert result["rows_upserted"] == 1
        row = db.query("delegation_events")[0]
        assert row["correlation_id"] == "4ae8556b-af7c-4e85-a7f5-9388d60cebb5"
        assert row["tokens_input"] == 144
        assert row["tokens_output"] == 593
        assert row["cost_savings_usd"] == Decimal("0.009327")


class TestPromptResponseText:
    """OMN-10850 — prompt_text and response_text must be persisted to the row."""

    def test_prompt_and_response_text_written_to_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-prompt-response",
            task_type="code-review",
            delegated_to="agent-alpha",
            prompt_text="test prompt",
            response_text="test response",
        )
        HANDLER.project(event, db)
        rows = db.query("delegation_events")
        assert len(rows) == 1
        assert rows[0]["prompt_text"] == "test prompt"
        assert rows[0]["response_text"] == "test response"

    def test_prompt_response_text_default_none(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-no-text",
            task_type="code-review",
            delegated_to="agent-alpha",
        )
        HANDLER.project(event, db)
        rows = db.query("delegation_events")
        assert len(rows) == 1
        assert rows[0]["prompt_text"] is None
        assert rows[0]["response_text"] is None

    def test_prompt_response_text_via_handle_protocol(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "correlation_id": "corr-handle-text",
            "task_type": "summarize",
            "delegated_to": "agent-beta",
            "prompt_text": "test prompt",
            "response_text": "test response",
            "_db": db,
        }
        result = HANDLER.handle(payload)
        assert result["rows_upserted"] == 1
        rows = db.query("delegation_events")
        assert rows[0]["prompt_text"] == "test prompt"
        assert rows[0]["response_text"] == "test response"


class TestCostFields:
    """Cost fields from task-delegated events are dashboard-critical."""

    def test_cost_fields_written_to_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-costs",
            task_type="code-review",
            delegated_to="agent-alpha",
            cost_usd=0.001,
            cost_savings_usd=0.123,
        )
        HANDLER.project(event, db)
        rows = db.query("delegation_events")
        assert len(rows) == 1
        assert rows[0]["cost_usd"] == 0.001
        assert rows[0]["cost_savings_usd"] == 0.123

    def test_cost_fields_via_handle_protocol(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "correlation_id": "corr-costs-handle",
            "task_type": "summarize",
            "delegated_to": "agent-beta",
            "cost_usd": 0.0,
            "cost_savings_usd": 0.456,
            "_db": db,
        }
        result = HANDLER.handle(payload)
        assert result["rows_upserted"] == 1
        rows = db.query("delegation_events")
        assert rows[0]["cost_usd"] == 0.0
        assert rows[0]["cost_savings_usd"] == 0.456


class TestPricingManifestVersion:
    """OMN-10949 — projection writes pricing_manifest_version; defaults to 0 for old events."""

    def test_pricing_version_written_to_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-pricing-v",
            task_type="code-review",
            delegated_to="agent-alpha",
            pricing_manifest_version=3,
        )
        HANDLER.project(event, db)
        rows = db.query("delegation_events")
        assert len(rows) == 1
        assert rows[0]["pricing_manifest_version"] == 3

    def test_pricing_version_defaults_to_zero(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-pricing-default",
            task_type="code-review",
            delegated_to="agent-alpha",
        )
        HANDLER.project(event, db)
        rows = db.query("delegation_events")
        assert len(rows) == 1
        assert rows[0]["pricing_manifest_version"] == 0

    def test_pricing_version_via_handle_protocol(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "correlation_id": "corr-pricing-handle",
            "task_type": "summarize",
            "delegated_to": "agent-beta",
            "pricing_manifest_version": 5,
            "_db": db,
        }
        result = HANDLER.handle(payload)
        assert result["rows_upserted"] == 1
        rows = db.query("delegation_events")
        assert rows[0]["pricing_manifest_version"] == 5

    def test_old_event_without_field_defaults_to_zero(self) -> None:
        """Events emitted before OMN-10949 (no pricing_manifest_version) default to 0."""
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "correlation_id": "corr-legacy",
            "task_type": "code-review",
            "delegated_to": "agent-gamma",
            # pricing_manifest_version intentionally absent
            "_db": db,
        }
        result = HANDLER.handle(payload)
        assert result["rows_upserted"] == 1
        rows = db.query("delegation_events")
        assert rows[0]["pricing_manifest_version"] == 0


class TestComplianceCounters:
    """OMN-10793 — projection writes tokens_to_compliance and compliance_attempts
    from the inbound event payload to the delegation_events row. The defaults
    (0 tokens, 1 attempt) cover the legacy emitters that haven't yet wired
    the counters into their payload."""

    def test_event_carries_compliance_counters_to_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-compliance",
            task_type="code-review",
            delegated_to="agent-alpha",
            tokens_to_compliance=540,
            compliance_attempts=2,
        )
        HANDLER.project(event, db)
        rows = db.query("delegation_events")
        assert len(rows) == 1
        assert rows[0]["tokens_to_compliance"] == 540
        assert rows[0]["compliance_attempts"] == 2

    def test_compliance_counters_default_when_event_omits_them(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-defaults",
            task_type="code-review",
            delegated_to="agent-beta",
        )
        HANDLER.project(event, db)
        rows = db.query("delegation_events")
        assert len(rows) == 1
        # Defaults: zero tokens consumed, single attempt = first-try compliance.
        assert rows[0]["tokens_to_compliance"] == 0
        assert rows[0]["compliance_attempts"] == 1

    def test_dict_payload_with_counters_via_handle_protocol(self) -> None:
        # The runtime invokes handle(input_data) — confirm the protocol shim
        # threads the compliance fields end-to-end (dict -> model -> row).
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "correlation_id": "corr-protocol",
            "task_type": "summarize",
            "delegated_to": "agent-gamma",
            "tokens_to_compliance": 1280,
            "compliance_attempts": 3,
            "_db": db,
        }
        result = HANDLER.handle(payload)
        assert result["rows_upserted"] == 1
        rows = db.query("delegation_events")
        assert rows[0]["tokens_to_compliance"] == 1280
        assert rows[0]["compliance_attempts"] == 3


class TestTerminalEventEmission:
    """OMN-11187 — after a successful DB write the runner must emit to the terminal topic."""

    def _make_inmemory_runner(self) -> tuple[Any, list[tuple[str, bytes]]]:
        """Build a DelegationProjectionRunner with an in-memory DB and a capture publish_fn."""
        from unittest.mock import AsyncMock, MagicMock

        from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        published: list[tuple[str, bytes]] = []

        async def capture_publish(topic: str, value: bytes) -> None:
            published.append((topic, value))

        runner = DelegationProjectionRunner(publish_fn=capture_publish)
        # Replace the DB adapter with a mock that no-ops execute
        mock_db = MagicMock(spec=AsyncpgAdapter)
        mock_db.execute = AsyncMock(return_value=None)
        runner._db = mock_db
        return runner, published

    def test_terminal_event_emitted_after_task_delegated(self) -> None:
        import asyncio
        import json

        from omnimarket.projection.runner import MessageMeta

        runner, published = self._make_inmemory_runner()
        topic = runner.subscribe_topics[0]
        data = {
            "correlation_id": "corr-terminal-001",
            "task_type": "code-review",
            "delegated_to": "agent-alpha",
        }
        meta = MessageMeta(partition=0, offset=0, fallback_id="corr-terminal-001")

        asyncio.run(runner.project_event(topic, data, meta))

        assert len(published) == 1
        terminal_topic, raw = published[0]
        assert terminal_topic == "onex.evt.omnimarket.projection-delegation-applied.v1"
        envelope = json.loads(raw.decode("utf-8"))
        assert envelope["correlation_id"] == "corr-terminal-001"
        assert (
            envelope["event_type"]
            == "onex.evt.omnimarket.projection-delegation-applied.v1"
        )

    def test_terminal_event_carries_source_topic(self) -> None:
        import asyncio
        import json

        from omnimarket.projection.runner import MessageMeta

        runner, published = self._make_inmemory_runner()
        topic = runner.subscribe_topics[0]
        data = {
            "correlation_id": "corr-source-topic",
            "task_type": "refactor",
            "delegated_to": "agent-beta",
        }
        meta = MessageMeta(partition=0, offset=1, fallback_id="corr-source-topic")

        asyncio.run(runner.project_event(topic, data, meta))

        assert len(published) == 1
        envelope = json.loads(published[0][1].decode("utf-8"))
        assert envelope["payload"]["source_topic"] == topic

    def test_no_terminal_event_when_publish_fn_is_none_and_no_brokers(self) -> None:
        """Without KAFKA_BROKERS and no publish_fn, emission is skipped gracefully."""
        import asyncio
        import os
        from unittest.mock import AsyncMock, MagicMock

        from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        env_backup = os.environ.pop("KAFKA_BROKERS", None)
        try:
            runner = DelegationProjectionRunner()  # no publish_fn
            mock_db = MagicMock(spec=AsyncpgAdapter)
            mock_db.execute = AsyncMock(return_value=None)
            runner._db = mock_db

            from omnimarket.projection.runner import MessageMeta

            topic = runner.subscribe_topics[0]
            data = {
                "correlation_id": "corr-no-publish",
                "task_type": "code-review",
                "delegated_to": "agent-gamma",
            }
            meta = MessageMeta(partition=0, offset=2, fallback_id="corr-no-publish")
            # Should not raise even without Kafka
            ok = asyncio.run(runner.project_event(topic, data, meta))
            assert ok is True
        finally:
            if env_backup is not None:
                os.environ["KAFKA_BROKERS"] = env_backup

    def test_terminal_event_topic_read_from_contract(self) -> None:

        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        runner = DelegationProjectionRunner()
        assert (
            runner._terminal_topic
            == "onex.evt.omnimarket.projection-delegation-applied.v1"
        )
