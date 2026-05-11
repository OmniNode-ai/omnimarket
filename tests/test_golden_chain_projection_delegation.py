"""Golden chain tests for node_projection_delegation."""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionDelegation()


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
            "onex.evt.omniclaude.task-delegated.v1"
            in contract["event_bus"]["subscribe_topics"]
        )


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
