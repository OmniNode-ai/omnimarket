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
        assert "onex.evt.omnibase-infra.delegation-completed.v1" in topics
        assert "onex.evt.omnibase-infra.delegation-failed.v1" in topics

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
            "pricing_manifest_version": 1,
        }

        result = HANDLER.handle(payload)

        assert result["rows_upserted"] == 1
        row = db.query("delegation_events")[0]
        assert row["correlation_id"] == "4ae8556b-af7c-4e85-a7f5-9388d60cebb5"
        assert row["quality_gates_checked"] == 1
        assert row["quality_gates_failed"] == 0
        assert row["quality_gates_checked_jsonb"] == ["delegate-skill-terminal"]
        assert row["quality_gates_failed_jsonb"] == []
        assert row["tokens_input"] == 144
        assert row["tokens_output"] == 593
        assert row["cost_savings_usd"] == Decimal("0.009327")
        assert row["pricing_manifest_version"] == 1

    def test_sparse_task_delegated_event_does_not_clear_terminal_evidence(self) -> None:
        db = InmemoryDatabaseAdapter()
        correlation_id = "4ae8556b-af7c-4e85-a7f5-9388d60cebb5"
        terminal_payload: dict[str, object] = {
            "_db": db,
            "_event_type": "delegate-skill-completed",
            "status": "completed",
            "correlation_id": correlation_id,
            "task_type": "test",
            "provider": "local-qwen",
            "model_name": _DELEGATE_SKILL_TEST_MODEL,
            "prompt_text": "write useful unit tests",
            "response": "useful pytest proof",
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
        }
        sparse_compat_payload: dict[str, object] = {
            "_db": db,
            "_event_type": "task-delegated",
            "correlation_id": correlation_id,
            "task_type": "test",
            "delegated_to": _DELEGATE_SKILL_TEST_MODEL,
            "model_name": _DELEGATE_SKILL_TEST_MODEL,
            "quality_gate_passed": True,
        }

        HANDLER.handle(terminal_payload)
        HANDLER.handle(sparse_compat_payload)

        row = db.query("delegation_events")[0]
        assert row["prompt_text"] == "write useful unit tests"
        assert row["response_text"] == "useful pytest proof"
        assert row["tokens_input"] == 144
        assert row["tokens_output"] == 593
        assert row["tokens_to_compliance"] == 737
        assert row["cost_savings_usd"] == Decimal("0.009327")
        assert row["pricing_manifest_version"] == 1

    def test_sync_handler_projects_canonical_delegation_terminal_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "_db": db,
            "_event_type": "onex.evt.omnibase-infra.delegation-completed.v1",
            "correlation_id": "corr-canonical-terminal",
            "task_type": "test",
            "model_used": "Qwen3-Coder-30B-A3B",
            "content": "projection proof",
            "quality_passed": True,
            "quality_score": 0.98,
            "latency_ms": 1200,
            "prompt_tokens": 144,
            "completion_tokens": 593,
            "total_tokens": 737,
            "fallback_to_claude": False,
            "tokens_to_compliance": 737,
            "compliance_attempts": 1,
        }

        result = HANDLER.handle(payload)

        assert result["rows_upserted"] == 1
        row = db.query("delegation_events")[0]
        assert row["correlation_id"] == "corr-canonical-terminal"
        assert row["delegated_to"] == "Qwen3-Coder-30B-A3B"
        assert row["model_name"] == "Qwen3-Coder-30B-A3B"
        assert row["quality_gate_passed"] is True
        assert row["tokens_input"] == 144
        assert row["tokens_output"] == 593
        assert row["tokens_to_compliance"] == 737
        assert row["response_text"] == "projection proof"

    def test_terminal_row_carries_created_at(self) -> None:
        """OMN-13171: the terminal projection row populates created_at.

        The deployed delegation_events schema declares created_at as
        NOT NULL. The projection write must inject created_at explicitly so a
        backing store without an implicit DB default (e.g. the local SQLite
        evidence target on a warm volume) does not raise a NOT NULL constraint.
        """
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "_db": db,
            "_event_type": "delegate-skill-completed",
            "status": "completed",
            "correlation_id": "1d8f9a02-5b6c-4d7e-8f10-2a3b4c5d6e7f",
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
                "latency_ms": 1250,
                "cost_usd": 0.0,
                "cost_savings_usd": 0.0,
            },
        }

        result = HANDLER.handle(payload)

        assert result["rows_upserted"] == 1
        row = db.query("delegation_events")[0]
        created_at = row.get("created_at")
        assert created_at, "terminal projection row must populate created_at"
        # created_at mirrors the event timestamp (explicit injection, deterministic),
        # not an implicit datetime.now() at the DB layer.
        assert created_at == row["timestamp"]

    def test_terminal_write_to_sqlite_with_not_null_created_at(
        self, tmp_path: Path
    ) -> None:
        """OMN-13171: terminal write to a NOT NULL created_at SQLite store succeeds.

        Reproduces the local-delegate evidence path against a warm-volume schema
        where delegation_events.created_at is NOT NULL with no DB default. Before
        the fix the INSERT raised sqlite3.IntegrityError: NOT NULL constraint
        failed: delegation_events.created_at.
        """
        import sqlite3

        from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
            ModelDelegateSkillTerminalProjection,
        )
        from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

        db_path = tmp_path / "delegation.sqlite"
        # Seed the deployed-shape table: created_at NOT NULL, no default — the
        # warm-volume scenario the SqliteDatabaseAdapter additive DDL cannot relax.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "CREATE TABLE delegation_events ("
                "correlation_id TEXT NOT NULL UNIQUE, "
                "created_at TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

        adapter = SqliteDatabaseAdapter(db_path)
        terminal = ModelDelegateSkillTerminalProjection.from_payload(
            {
                "status": "completed",
                "correlation_id": "2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f80",
                "task_type": "code_generation",
                "provider": "local-qwen",
                "model_name": _DELEGATE_SKILL_TEST_MODEL,
                "response": "evidence proof",
                "quality_gate_passed": True,
                "quality_gates_failed": [],
                "metrics": {
                    "input_tokens": 11,
                    "output_tokens": 22,
                    "total_tokens": 33,
                    "latency_ms": 42,
                    "cost_usd": 0.0,
                    "cost_savings_usd": 0.0,
                },
            }
        )

        result = HANDLER.project_delegate_skill_terminal(terminal, adapter)

        assert result.rows_upserted == 1
        rows = adapter.query(
            "delegation_events",
            {"correlation_id": "2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f80"},
        )
        assert len(rows) == 1
        assert rows[0]["created_at"]

    def test_dashboard_projection_views_are_declared_by_migrations(self) -> None:
        delegation_view_migration = Path(
            "src/omnimarket/nodes/node_projection_delegation/migrations/"
            "0010_create_delegation_dashboard_projection_views.sql"
        ).read_text()
        savings_view_migration = Path(
            "src/omnimarket/nodes/node_projection_savings/migrations/"
            "076_create_delegation_savings_projection_view.sql"
        ).read_text()

        assert (
            "CREATE OR REPLACE VIEW projection_delegation_summary"
            in delegation_view_migration
        )
        assert (
            "CREATE OR REPLACE VIEW projection_delegation_model_routing"
            in delegation_view_migration
        )
        assert (
            "CREATE OR REPLACE VIEW projection_delegation_quality_gate"
            in delegation_view_migration
        )
        assert (
            "CREATE OR REPLACE VIEW projection_delegation_token_usage"
            in delegation_view_migration
        )
        assert (
            "CREATE OR REPLACE VIEW projection_delegation_savings"
            in savings_view_migration
        )


class TestGenerationCompletedProjection:
    """OMN-12800 — the live runtime dispatches HandlerProjectionDelegation.handle()
    for the node-generation-completed topic (the contract `handler:` field). The
    handler must project that event into the generation_events table rather than
    falling through to ModelTaskDelegatedEvent and raising ValidationError.

    The auto-wiring path derives _event_type as the topic's penultimate segment
    (omnibase_infra .../runtime/auto_wiring/handler_wiring.py::
    _derive_projection_event_type), i.e. "node-generation-completed" for
    onex.evt.omnimarket.node-generation-completed.v1. These tests dispatch with
    exactly that value.
    """

    _EVENT_TYPE = "node-generation-completed"

    def _generation_payload(
        self, db: InmemoryDatabaseAdapter, **overrides: object
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "_db": db,
            "_event_type": self._EVENT_TYPE,
            "correlation_id": "gen-corr-001",
            "task_description": "Build a node that classifies tickets",
            "provider": "local-qwen",
            "model_id": "Qwen3-Coder-30B-A3B",
            "endpoint_class": "local",
            "attempt_count": 1,
            "total_latency_e2e_ms": 3200,
            "contract_passed": True,
            "cost_inference_usd": 0.0,
            "contract_yaml": "name: node_ticket_classifier\ncontract_version: 1.0.0\n",
            "handler_source": "def handle(input_data):\n    return {}\n",
        }
        payload.update(overrides)
        return payload

    def test_generation_event_projects_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = HANDLER.handle(self._generation_payload(db))

        assert result["rows_upserted"] == 1
        rows = db.query("generation_events")
        assert len(rows) == 1, "generation-completed event must write generation_events"
        row = rows[0]
        assert row["correlation_id"] == "gen-corr-001"
        assert row["task_description"] == "Build a node that classifies tickets"
        assert row["provider"] == "local-qwen"
        assert row["model_id"] == "Qwen3-Coder-30B-A3B"
        assert row["contract_passed"] is True

    def test_generation_event_persists_contract_yaml_and_handler_source(self) -> None:
        db = InmemoryDatabaseAdapter()
        contract_yaml = "name: node_big\n" + ("# padding\n" * 10_000)
        handler_source = "def handle(input_data):\n    return {'ok': True}\n"
        HANDLER.handle(
            self._generation_payload(
                db, contract_yaml=contract_yaml, handler_source=handler_source
            )
        )

        row = db.query("generation_events")[0]
        # No truncation: the full payload round-trips intact (OMN-12780 Wave 1C).
        assert row["contract_yaml"] == contract_yaml
        assert row["handler_source"] == handler_source

    def test_generation_event_does_not_write_delegation_events(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.handle(self._generation_payload(db))

        # The generation branch must NOT pollute delegation_events.
        assert db.query("delegation_events") == []

    def test_generation_event_dedup_by_correlation_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.handle(self._generation_payload(db, contract_yaml="name: first\n"))
        HANDLER.handle(self._generation_payload(db, contract_yaml="name: second\n"))

        rows = db.query("generation_events")
        assert len(rows) == 1, "duplicate correlation_id must dedup to one row"

    def test_empty_output_persisted_as_empty_string(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.handle(
            self._generation_payload(db, contract_yaml="", handler_source="")
        )

        row = db.query("generation_events")[0]
        # Empty string is the failed-generation sentinel; never coerced to NULL.
        assert row["contract_yaml"] == ""
        assert row["handler_source"] == ""


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


class TestZeroTokenZeroCostTerminal:
    """OMN-13121 — a well-formed zero-token/zero-cost terminal must materialize a
    row. Zero tokens + zero cost is the steady state for free local-LLM delegation
    and golden-chain proofs, not a malformed event. The prior OMN-11923 guard
    silently dropped these (rows_upserted=0, no upsert, no raise), stranding the
    organic delegation tail at zero rows."""

    _CORR_ZERO = "00000000-0000-0000-0000-000000000001"
    _CORR_REAL = "00000000-0000-0000-0000-000000000002"
    _CORR_COST = "00000000-0000-0000-0000-000000000003"
    _SESSION = "00000000-0000-0000-0000-000000000099"

    def _make_zero_token_terminal_payload(
        self, correlation_id: str
    ) -> dict[str, object]:
        return {
            "_event_type": "delegate-skill-completed",
            "status": "completed",
            "correlation_id": correlation_id,
            "session_id": self._SESSION,
            "task_type": "test",
            "provider": "local-qwen",
            "model_name": "test-model",
            "response": "ok",
            "quality_gate_passed": True,
            "quality_gates_failed": [],
            "metrics": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "tokens_to_compliance": 0,
                "compliance_attempts": 1,
                "cost_usd": 0.0,
                "cost_savings_usd": 0.0,
                "latency_ms": 100,
            },
            "pricing_manifest_version": 0,
        }

    def test_zero_token_terminal_event_upserts_exactly_one_row(self) -> None:
        """Row-delta proof: before=0 rows, publish a zero-token/zero-cost
        terminal, after=exactly 1 row (OMN-13121 foundational pattern)."""
        db = InmemoryDatabaseAdapter()
        # before: projection table is empty.
        assert db.query("delegation_events") == []

        payload = self._make_zero_token_terminal_payload(self._CORR_ZERO)
        payload["_db"] = db
        result = HANDLER.handle(payload)

        # after: the zero-value terminal materialized exactly one row.
        assert result["rows_upserted"] == 1
        rows = db.query("delegation_events")
        assert len(rows) == 1
        row = rows[0]
        assert row["correlation_id"] == self._CORR_ZERO
        assert row["tokens_input"] == 0
        assert row["tokens_output"] == 0
        assert row["cost_usd"] == Decimal("0")

    def test_real_token_terminal_event_accepted(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "_db": db,
            "_event_type": "delegate-skill-completed",
            "status": "completed",
            "correlation_id": self._CORR_REAL,
            "session_id": self._SESSION,
            "task_type": "test",
            "provider": "local-qwen",
            "model_name": "test-model",
            "response": "ok",
            "quality_gate_passed": True,
            "quality_gates_failed": [],
            "metrics": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "tokens_to_compliance": 150,
                "compliance_attempts": 1,
                "cost_usd": 0.0,
                "cost_savings_usd": 0.005,
                "latency_ms": 500,
            },
            "pricing_manifest_version": 1,
        }
        result = HANDLER.handle(payload)
        assert result["rows_upserted"] == 1
        assert len(db.query("delegation_events")) == 1

    def test_zero_tokens_but_nonzero_cost_accepted(self) -> None:
        """Events with cost data but zero tokens are real (cost-only tracking)."""
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "_db": db,
            "_event_type": "delegate-skill-completed",
            "status": "completed",
            "correlation_id": self._CORR_COST,
            "session_id": self._SESSION,
            "task_type": "test",
            "provider": "local-qwen",
            "model_name": "test-model",
            "response": "ok",
            "quality_gate_passed": True,
            "quality_gates_failed": [],
            "metrics": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "tokens_to_compliance": 0,
                "compliance_attempts": 1,
                "cost_usd": 0.001,
                "cost_savings_usd": 0.0,
                "latency_ms": 200,
            },
            "pricing_manifest_version": 1,
        }
        result = HANDLER.handle(payload)
        assert result["rows_upserted"] == 1
        assert len(db.query("delegation_events")) == 1


class TestNoBackfillMaterialization:
    """OMN-12606 — a fresh terminal delegation event must materialize into
    delegation_events via the reducer/orchestrator completion path with NO
    manual operator backfill (May 31 finding), and the materialized row must
    carry projection_version and reducer_version (OMN-12488 acceptance-extension).
    """

    _CORR = "9f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f"
    _SESSION = "1a2b3c4d-5e6f-4708-9a0b-1c2d3e4f5061"

    def _fresh_terminal_payload(self) -> dict[str, object]:
        """A fresh, never-before-seen terminal delegation event."""
        return {
            "_event_type": "delegate-skill-completed",
            "status": "completed",
            "correlation_id": self._CORR,
            "session_id": self._SESSION,
            "task_type": "test",
            "provider": "local-qwen",
            "model_name": _DELEGATE_SKILL_TEST_MODEL,
            "response": "fresh materialization proof",
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
        }

    def test_fresh_terminal_event_materializes_row_without_backfill(self) -> None:
        """The reducer materializes the row directly from the terminal event.

        No operator UPDATE/INSERT touches the table other than the reducer's
        own upsert — the only write recorded on the in-memory DB is the
        single reducer upsert for the fresh correlation_id.
        """
        db = InmemoryDatabaseAdapter()
        payload = self._fresh_terminal_payload()
        payload["_db"] = db

        result = HANDLER.handle(payload)

        assert result["rows_upserted"] == 1
        rows = db.query("delegation_events")
        assert len(rows) == 1
        # Exactly one write hit the table: the reducer's own upsert. A second
        # write would indicate an operator backfill path is still required.
        assert db.upsert_count == 1
        assert rows[0]["correlation_id"] == self._CORR

    def test_materialized_row_carries_version_fields(self) -> None:
        """OMN-12488 acceptance-extension: projection_version + reducer_version."""
        from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
            PROJECTION_VERSION,
            REDUCER_VERSION,
        )

        db = InmemoryDatabaseAdapter()
        payload = self._fresh_terminal_payload()
        payload["_db"] = db

        HANDLER.handle(payload)

        row = db.query("delegation_events")[0]
        assert row["projection_version"] == PROJECTION_VERSION
        assert row["reducer_version"] == REDUCER_VERSION

    def test_async_runner_materializes_row_with_version_fields(self) -> None:
        """The async DelegationProjectionRunner path also emits version fields."""
        import asyncio

        from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
            PROJECTION_VERSION,
            REDUCER_VERSION,
        )
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )
        from omnimarket.projection.runner import MessageMeta

        captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

        class _RecordingDB:
            async def execute(self, *args: object, **kwargs: object) -> None:
                captured.append((args, kwargs))

        runner = DelegationProjectionRunner()
        runner._db = _RecordingDB()  # type: ignore[assignment]

        topic = runner._topic_delegate_skill_completed
        assert topic, "contract must declare a delegate-skill-completed topic"
        data = self._fresh_terminal_payload()
        data.pop("_event_type")
        meta = MessageMeta(partition=0, offset=0, fallback_id=self._CORR)

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True
        # Exactly one DB write: the reducer's own upsert, no backfill.
        assert len(captured) == 1
        sql = str(captured[0][0][0])
        params = captured[0][0][1:]
        assert "projection_version" in sql
        assert "reducer_version" in sql
        assert PROJECTION_VERSION in params
        assert REDUCER_VERSION in params

    def test_migration_declares_version_columns(self) -> None:
        migration = Path(
            "src/omnimarket/nodes/node_projection_delegation/migrations/"
            "0011_delegation_event_projection_versions.sql"
        ).read_text()
        assert "projection_version" in migration
        assert "reducer_version" in migration
