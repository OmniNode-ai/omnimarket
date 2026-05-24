# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain e2e test: routing chain (OMN-11768).

Chain:
  head topic : onex.evt.omniclaude.llm-routing-decision.v1
  tail table : llm_routing_decisions

Verifies that:
1. A llm-routing-decision event is correctly projected to llm_routing_decisions.
2. Duplicate correlation_id is idempotently upserted (dedup guarantee).
3. The contract subscribe_topics include the head topic (no hardcoded strings).
4. All expected golden chain fields are present in the projected row.
5. project_batch() handles multiple events correctly.
6. The handle() protocol shim threads fields end-to-end.

Related: OMN-11768, OMN-11759, OMN-8540
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_llm_routing.handlers.handler_projection_llm_routing import (
    HandlerProjectionLlmRouting,
    ModelLlmRoutingDecisionEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionLlmRouting()
_CONTRACT_PATH = Path("src/omnimarket/nodes/node_projection_llm_routing/contract.yaml")
_EXPECTED_JSON = Path(
    "tests/integration/golden_chain/expected_golden_chain_routing.json"
)


def _load_contract() -> dict[str, object]:
    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)
    assert isinstance(contract, dict)
    return contract


def _routing_decision_topic() -> str:
    contract = _load_contract()
    subscribe_topics = contract["event_bus"]["subscribe_topics"]
    assert isinstance(subscribe_topics, list)
    topic = subscribe_topics[0]
    assert isinstance(topic, str)
    return topic


@pytest.mark.unit
class TestRoutingProjection:
    def test_project_single_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelLlmRoutingDecisionEvent(
            correlation_id="corr-routing-001",
            session_id="sess-abc",
            llm_agent="agent-api-architect",
            fuzzy_agent="agent-api-architect",
            agreement=True,
            llm_confidence=0.92,
            fuzzy_confidence=0.87,
            llm_latency_ms=120,
            fuzzy_latency_ms=5,
            used_fallback=False,
            routing_prompt_version="v3.1.0",
            intent="code_review",
            model="qwen3-coder-30b",
            cost_usd=0.00042,
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("llm_routing_decisions")
        assert len(rows) == 1
        row = rows[0]
        assert row["correlation_id"] == "corr-routing-001"
        assert row["llm_agent"] == "agent-api-architect"
        assert row["agreement"] is True
        assert row["used_fallback"] is False
        assert row["routing_prompt_version"] == "v3.1.0"

    def test_dedup_by_correlation_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelLlmRoutingDecisionEvent(
                correlation_id="corr-routing-dedup",
                llm_agent="agent-alpha",
                routing_prompt_version="v1.0.0",
            ),
            db,
        )
        HANDLER.project(
            ModelLlmRoutingDecisionEvent(
                correlation_id="corr-routing-dedup",
                llm_agent="agent-beta",
                routing_prompt_version="v2.0.0",
            ),
            db,
        )
        rows = db.query("llm_routing_decisions")
        assert len(rows) == 1
        assert rows[0]["llm_agent"] == "agent-beta"
        assert rows[0]["routing_prompt_version"] == "v2.0.0"

    def test_project_batch(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [
            ModelLlmRoutingDecisionEvent(
                correlation_id=f"corr-batch-{i:03d}",
                llm_agent=f"agent-{i}",
            )
            for i in range(3)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 3
        assert len(db.query("llm_routing_decisions")) == 3

    def test_fallback_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelLlmRoutingDecisionEvent(
            correlation_id="corr-fallback-001",
            llm_agent="agent-fallback",
            fuzzy_agent=None,
            agreement=False,
            used_fallback=True,
        )
        HANDLER.project(event, db)
        rows = db.query("llm_routing_decisions", {"used_fallback": True})
        assert len(rows) == 1
        assert rows[0]["fuzzy_agent"] is None

    def test_selected_agent_alias(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "correlation_id": "corr-alias-001",
            "selected_agent": "agent-via-alias",
            "_db": db,
        }
        result = HANDLER.handle(payload)
        assert result["rows_upserted"] == 1
        rows = db.query("llm_routing_decisions")
        assert rows[0]["llm_agent"] == "agent-via-alias"

    def test_handle_protocol_shim(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "correlation_id": "corr-handle-001",
            "session_id": "sess-xyz",
            "llm_agent": "agent-designer",
            "fuzzy_agent": "agent-reviewer",
            "agreement": False,
            "llm_confidence": 0.75,
            "used_fallback": False,
            "routing_prompt_version": "v2.0.0",
            "intent": "design_review",
            "model": "deepseek-r1-14b",
            "cost_usd": 0.00012,
            "_db": db,
        }
        result = HANDLER.handle(payload)
        assert result["rows_upserted"] == 1
        row = db.query("llm_routing_decisions")[0]
        assert row["session_id"] == "sess-xyz"
        assert row["llm_confidence"] == 0.75
        assert row["cost_usd"] == 0.00012
        assert row["model"] == "deepseek-r1-14b"

    def test_event_bus_wiring(self) -> None:
        contract = _load_contract()
        subscribe_topics = contract["event_bus"]["subscribe_topics"]
        routing_topic = _routing_decision_topic()
        assert routing_topic in subscribe_topics
        handler_cfg = contract["handler"]
        assert (
            handler_cfg["module"]
            == "omnimarket.nodes.node_projection_llm_routing.handlers.handler_projection_llm_routing"
        )
        assert handler_cfg["class"] == "HandlerProjectionLlmRouting"
        routing_handlers = contract["handler_routing"]["handlers"]
        assert routing_handlers[0]["topic"] == routing_topic
        assert routing_handlers[0]["handler"]["name"] == "HandlerProjectionLlmRouting"

    def test_expected_golden_chain_fixture(self) -> None:
        expected = json.loads(_EXPECTED_JSON.read_text())
        assert expected["chain"] == "routing"
        assert expected["head_topic"] == _routing_decision_topic()
        assert expected["tail_table"] == "llm_routing_decisions"
        assert "correlation_id" in expected["expected_fields"]

    def test_null_optional_fields_accepted(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelLlmRoutingDecisionEvent(
            correlation_id="corr-nulls-001",
            llm_agent="agent-minimal",
        )
        HANDLER.project(event, db)
        row = db.query("llm_routing_decisions")[0]
        assert row["fuzzy_agent"] is None
        assert row["llm_confidence"] is None
        assert row["fuzzy_confidence"] is None
        assert row["cost_usd"] is None
        assert row["session_id"] is None
        assert row["routing_prompt_version"] == "unknown"
