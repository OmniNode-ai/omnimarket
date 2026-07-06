"""Golden chain tests for node_projection_mcp_tools.

Tests cover:
- MCP-eligible registration events are projected into the mcp_tools table.
- Non-MCP events are acknowledged without writing rows.
- Idempotent UPSERT behaviour on repeated events for the same tool_name.
- Batch projection.
- Freshness state computation.
- Contract wiring (subscribe/publish topics, terminal_event, projection API, freshness_sla).
- Model immutability (frozen Pydantic models).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import yaml

from omnimarket.nodes.node_projection_mcp_tools.handlers.handler_projection_mcp_tools import (
    HandlerProjectionMcpTools,
    ModelMcpToolRegistrationEvent,
)
from omnimarket.nodes.node_projection_mcp_tools.models.enums import (
    EnumFreshnessState,
    EnumMcpToolStatus,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionMcpTools()

CONTRACT_PATH = "src/omnimarket/nodes/node_projection_mcp_tools/contract.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mcp_event(**overrides: object) -> ModelMcpToolRegistrationEvent:
    """Build a minimal mcp_eligible registration event."""
    defaults: dict[str, object] = {
        "node_name": "tool_code_review",
        "contract_hash": "abc123",
        "correlation_id": "corr-001",
        "status": "registered",
        "mcp_eligible": True,
        "mcp_tags": ("mcp-enabled", "mcp-tool:tool_code_review"),
        "emitted_at": datetime.now(UTC).isoformat(),
        "source_topic": "onex.evt.platform.node-registration.v1",
    }
    defaults.update(overrides)
    return ModelMcpToolRegistrationEvent(**defaults)


def _non_mcp_event(**overrides: object) -> ModelMcpToolRegistrationEvent:
    """Build a registration event for a non-MCP node."""
    defaults: dict[str, object] = {
        "node_name": "node_pure_compute",
        "mcp_eligible": False,
        "status": "registered",
        "emitted_at": datetime.now(UTC).isoformat(),
        "source_topic": "onex.evt.platform.node-registration.v1",
    }
    defaults.update(overrides)
    return ModelMcpToolRegistrationEvent(**defaults)


# ---------------------------------------------------------------------------
# MCP registration: happy path
# ---------------------------------------------------------------------------


class TestProjectionMcpToolsMcpEligible:
    def test_mcp_event_upserts_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _mcp_event()
        result = HANDLER.project(event, db)

        assert result.rows_upserted == 1
        assert result.tool_name == "tool_code_review"
        rows = db.query("mcp_tools")
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "tool_code_review"

    def test_mcp_event_sets_active_status(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _mcp_event()
        HANDLER.project(event, db)
        row = db.query("mcp_tools")[0]
        assert row["status"] == EnumMcpToolStatus.ACTIVE
        assert row["is_active"] is True

    def test_mcp_tags_stored(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _mcp_event(mcp_tags=("mcp-enabled", "mcp-tool:tool_code_review"))
        HANDLER.project(event, db)
        row = db.query("mcp_tools")[0]
        assert "mcp-enabled" in row["mcp_tags"]
        assert "mcp-tool:tool_code_review" in row["mcp_tags"]

    def test_correlation_id_stored(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _mcp_event(correlation_id="test-corr-999")
        HANDLER.project(event, db)
        row = db.query("mcp_tools")[0]
        assert row["correlation_id"] == "test-corr-999"

    def test_description_from_contract_metadata(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _mcp_event(
            contract_metadata={"description": "Runs automated code review"}
        )
        HANDLER.project(event, db)
        row = db.query("mcp_tools")[0]
        assert row["description"] == "Runs automated code review"

    def test_model_id_from_contract_metadata(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _mcp_event(contract_metadata={"model_id": "deepseek-r1-14b"})
        HANDLER.project(event, db)
        row = db.query("mcp_tools")[0]
        assert row["model_id"] == "deepseek-r1-14b"

    def test_model_id_alias_model_id_camel_case(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _mcp_event(contract_metadata={"modelId": "qwen3-coder"})
        HANDLER.project(event, db)
        row = db.query("mcp_tools")[0]
        assert row["model_id"] == "qwen3-coder"


# ---------------------------------------------------------------------------
# Real producer wire shape (OMN-14005)
#
# node_generation_consumer._emit_registration sends a generic `tags: list[str]`
# field (e.g. "mcp-enabled", "mcp-tool:<name>") — it deliberately does NOT send
# mcp_eligible/mcp_tags (removed as legacy, see
# test_registration_payload_tags_are_mcp_conformant in node_generation_consumer's
# tests). Before the _derive_mcp_eligibility_from_tags model_validator, every
# real generation-sourced registration event silently acked with
# rows_upserted=0 because mcp_eligible defaulted False.
# ---------------------------------------------------------------------------


class TestProjectionMcpToolsRealProducerShape:
    def test_real_producer_tags_derive_mcp_eligible_and_upsert(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelMcpToolRegistrationEvent(
            event_type="registered",
            correlation_id="corr-gen-001",
            node_name="node_stub_compute",
            service_name="node_stub_compute",
            tags=[
                "mcp-enabled",
                "node-type:orchestrator",
                "mcp-tool:node_stub_compute",
            ],
            source="node_generation_consumer",
        )
        assert event.mcp_eligible is True
        assert "mcp-enabled" in event.mcp_tags

        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        row = db.query("mcp_tools")[0]
        assert row["tool_name"] == "node_stub_compute"
        assert row["correlation_id"] == "corr-gen-001"
        assert "mcp-enabled" in row["mcp_tags"]

    def test_real_producer_tags_without_mcp_enabled_stays_ineligible(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelMcpToolRegistrationEvent(
            node_name="node_pure_compute",
            tags=["node-type:compute"],
        )
        assert event.mcp_eligible is False
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 0
        assert db.query("mcp_tools") == []

    def test_explicit_mcp_eligible_wins_over_tags_derivation(self) -> None:
        """An explicit mcp_eligible/mcp_tags is never overridden by `tags`."""
        event = ModelMcpToolRegistrationEvent(
            node_name="tool_explicit",
            tags=["node-type:compute"],  # would derive mcp_eligible=False
            mcp_eligible=True,
            mcp_tags=("mcp-enabled",),
        )
        assert event.mcp_eligible is True
        assert event.mcp_tags == ("mcp-enabled",)

    def test_absent_tags_field_is_unaffected(self) -> None:
        """No `tags` key at all: existing mcp_eligible default behaviour holds."""
        event = ModelMcpToolRegistrationEvent(node_name="tool_no_tags")
        assert event.mcp_eligible is False
        assert event.mcp_tags == ()


# ---------------------------------------------------------------------------
# Non-MCP events: acknowledged but no write
# ---------------------------------------------------------------------------


class TestProjectionMcpToolsNonMcp:
    def test_non_mcp_event_produces_no_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _non_mcp_event()
        result = HANDLER.project(event, db)

        assert result.rows_upserted == 0
        assert db.query("mcp_tools") == []

    def test_non_mcp_result_reports_correct_tool_name(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _non_mcp_event(node_name="node_pure_compute")
        result = HANDLER.project(event, db)
        assert result.tool_name == "node_pure_compute"

    def test_non_mcp_result_freshness_fresh(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _non_mcp_event()
        result = HANDLER.project(event, db)
        assert result.freshness_state == EnumFreshnessState.FRESH


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestProjectionMcpToolsIdempotency:
    def test_same_tool_name_idempotent_upsert(self) -> None:
        db = InmemoryDatabaseAdapter()
        # Use contract_metadata to carry description.
        e1 = _mcp_event(node_name="tool_dup", contract_metadata={"description": "v1"})
        e2 = _mcp_event(node_name="tool_dup", contract_metadata={"description": "v2"})
        HANDLER.project(e1, db)
        HANDLER.project(e2, db)

        rows = db.query("mcp_tools")
        assert len(rows) == 1
        assert rows[0]["description"] == "v2"

    def test_different_tool_names_create_separate_rows(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(_mcp_event(node_name="tool_a"), db)
        HANDLER.project(_mcp_event(node_name="tool_b"), db)
        rows = db.query("mcp_tools")
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Rejected status
# ---------------------------------------------------------------------------


class TestProjectionMcpToolsRejected:
    def test_rejected_status_projects_inactive(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _mcp_event(status="rejected")
        HANDLER.project(event, db)
        row = db.query("mcp_tools")[0]
        assert row["status"] == EnumMcpToolStatus.REJECTED
        assert row["is_active"] is False


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


class TestProjectionMcpToolsFreshness:
    def test_fresh_when_lag_within_sla(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _mcp_event(emitted_at=datetime.now(UTC).isoformat())
        result = HANDLER.project(event, db)
        assert result.freshness_state == EnumFreshnessState.FRESH

    def test_stale_when_lag_between_max_and_degraded(self) -> None:
        db = InmemoryDatabaseAdapter()
        stale_ts = (datetime.now(UTC) - timedelta(seconds=90)).isoformat()
        event = _mcp_event(emitted_at=stale_ts)
        result = HANDLER.project(event, db)
        assert result.freshness_state == EnumFreshnessState.STALE

    def test_degraded_when_lag_exceeds_degraded_threshold(self) -> None:
        db = InmemoryDatabaseAdapter()
        old_ts = (datetime.now(UTC) - timedelta(seconds=150)).isoformat()
        event = _mcp_event(emitted_at=old_ts)
        result = HANDLER.project(event, db)
        assert result.freshness_state == EnumFreshnessState.DEGRADED

    def test_degraded_when_emitted_at_missing(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _mcp_event(emitted_at=None)
        result = HANDLER.project(event, db)
        assert result.freshness_state == EnumFreshnessState.DEGRADED

    def test_degraded_when_emitted_at_unparseable(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _mcp_event(emitted_at="not-a-timestamp")
        result = HANDLER.project(event, db)
        assert result.freshness_state == EnumFreshnessState.DEGRADED


# ---------------------------------------------------------------------------
# Dict interface (handle / handle_batch)
# ---------------------------------------------------------------------------


class TestProjectionMcpToolsHandleInterface:
    def test_handle_dict_mcp_eligible(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "_db": db,
            "node_name": "tool_handle_test",
            "mcp_eligible": True,
            "mcp_tags": ("mcp-enabled",),
            "emitted_at": datetime.now(UTC).isoformat(),
            "source_topic": "onex.evt.platform.node-registration.v1",
        }
        result = HANDLER.handle(payload)
        assert result["rows_upserted"] == 1
        assert result["tool_name"] == "tool_handle_test"

    def test_handle_dict_non_mcp(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "_db": db,
            "node_name": "node_silent",
            "mcp_eligible": False,
            "source_topic": "onex.evt.platform.node-registration.v1",
        }
        result = HANDLER.handle(payload)
        assert result["rows_upserted"] == 0

    def test_handle_raises_without_db_adapter(self) -> None:
        with pytest.raises(TypeError, match="DatabaseAdapter"):
            HANDLER.handle({"node_name": "x", "mcp_eligible": True})


# ---------------------------------------------------------------------------
# Batch projection
# ---------------------------------------------------------------------------


class TestProjectionMcpToolsBatch:
    def test_project_batch_mcp_eligible(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [_mcp_event(node_name=f"tool_{i:03d}") for i in range(4)]
        results = HANDLER.project_batch(events, db)
        assert len(results) == 4
        assert all(r.rows_upserted == 1 for r in results)
        assert len(db.query("mcp_tools")) == 4

    def test_project_batch_mixed(self) -> None:
        db = InmemoryDatabaseAdapter()
        events: list[ModelMcpToolRegistrationEvent] = [
            _mcp_event(node_name="tool_a"),
            _non_mcp_event(node_name="node_b"),
            _mcp_event(node_name="tool_c"),
        ]
        results = HANDLER.project_batch(events, db)
        assert results[0].rows_upserted == 1
        assert results[1].rows_upserted == 0
        assert results[2].rows_upserted == 1
        assert len(db.query("mcp_tools")) == 2


# ---------------------------------------------------------------------------
# Contract wiring
# ---------------------------------------------------------------------------


class TestProjectionMcpToolsContractWiring:
    def test_subscribe_topics_in_contract(self) -> None:
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        subs = contract["event_bus"]["subscribe_topics"]
        assert "onex.evt.platform.node-registration.v1" in subs

    def test_publish_topic_in_contract(self) -> None:
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        pubs = contract["event_bus"]["publish_topics"]
        assert pubs == ["onex.evt.omnimarket.projection-mcp-tools-applied.v1"]

    def test_terminal_event_in_contract(self) -> None:
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        assert (
            contract["terminal_event"]
            == "onex.evt.omnimarket.projection-mcp-tools-applied.v1"
        )

    def test_freshness_sla_in_contract(self) -> None:
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        sla = contract["freshness_sla"]
        assert sla["max_lag_seconds"] == 60
        assert sla["degraded_after_seconds"] == 120

    def test_node_type_is_reducer(self) -> None:
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        assert contract["node_type"] == "reducer"

    def test_db_io_table_is_mcp_tools(self) -> None:
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        tables = contract["db_io"]["db_tables"]
        assert any(t["name"] == "mcp_tools" for t in tables)

    def test_projection_api_topic(self) -> None:
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        assert "mcp-tools" in contract["projection_api"]["topic"]

    def test_projection_api_table(self) -> None:
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        assert contract["projection_api"]["table"] == "mcp_tools"


# ---------------------------------------------------------------------------
# Model immutability
# ---------------------------------------------------------------------------


class TestProjectionMcpToolsModels:
    def test_model_mcp_tool_projection_frozen(self) -> None:
        from omnimarket.nodes.node_projection_mcp_tools.models.model_mcp_tool_projection import (
            ModelMcpToolProjection,
        )

        row = ModelMcpToolProjection(
            tool_name="tool_x",
            registered_at="2026-06-28T00:00:00+00:00",
            projected_at="2026-06-28T00:00:01+00:00",
        )
        with pytest.raises(ValueError, match="frozen"):
            row.tool_name = "mutated"  # type: ignore[misc]

    def test_enum_values(self) -> None:
        assert EnumMcpToolStatus.ACTIVE == "active"
        assert EnumMcpToolStatus.INACTIVE == "inactive"
        assert EnumMcpToolStatus.REJECTED == "rejected"
        assert EnumFreshnessState.FRESH == "fresh"
        assert EnumFreshnessState.DEGRADED == "degraded"
