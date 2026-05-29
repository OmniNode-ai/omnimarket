# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression coverage for all-node market runtime dogfood inventory."""

from __future__ import annotations

from scripts.audit.market_node_runtime_dogfood import build_report


def test_market_node_runtime_dogfood_inventory_classifies_all_entry_points() -> None:
    report = build_report()
    summary = report["summary"]

    assert summary["node_dirs"] == 290
    assert summary["entry_points"] == 290
    assert summary["missing_entry_points"] == []
    assert summary["dangling_entry_points"] == []
    assert summary["routable"] >= 270
    assert summary["skipped"] == 16
    assert summary["failed"] <= 4
    assert summary["failure_buckets"] == {
        "missing_input_model": 4,
    }
    assert {
        item["node_name"]
        for item in report["failures"]
        if item["bucket"] == "missing_input_model"
    } == {
        "node_e2e_orchestrator",
        "node_emit_daemon",
        "node_overseer_observer",
        "node_projection_dep_health",
    }


def test_market_node_runtime_dogfood_proves_nested_contract_shapes() -> None:
    report = build_report()
    routable = {item["node_name"]: item for item in report["routable"]}

    assert (
        routable["node_ab_compare_orchestrator"]["input_model"]
        == "omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_start.ModelAbCompareStart"
    )
    assert (
        routable["node_loop_state_reducer"]["handler"]
        == "omnimarket.nodes.node_loop_state_reducer.handlers.handler_loop_state.HandlerLoopState"
    )
    assert (
        routable["node_adr_canary_orchestrator"]["input_model"]
        == "omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_request.ModelCanaryCommandPayload"
    )
    assert (
        routable["node_session_compose"]["command_topic"]
        == "onex.cmd.omnimarket.session-compose.v1"
    )
    assert (
        routable["node_ticket_query"]["terminal_topic"]
        == "onex.evt.omnimarket.ticket-query-completed.v1"
    )
    assert (
        routable["node_dirty_canonical_sweep"]["command_topic"]
        == "onex.cmd.omnimarket.dirty-canonical-sweep.v1"
    )
    assert (
        routable["node_code_embedding_effect"]["input_model"]
        == "omnimarket.nodes.node_code_embedding_effect.models.model_code_embedding_request.ModelCodeEmbeddingRequest"
    )
    assert (
        routable["node_projection_query"]["input_model"]
        == "omnimarket.nodes.node_projection_query.models.model_projection_query_request.ModelProjectionQueryRequest"
    )


def test_market_node_runtime_dogfood_proves_handler_key_repairs() -> None:
    report = build_report()
    routable = {item["node_name"]: item for item in report["routable"]}

    assert (
        routable["node_agent_coordinator_orchestrator"]["handler"]
        == "omnimemory.handlers.handler_subscription.HandlerSubscription"
    )
    assert (
        routable["node_memory_lifecycle_orchestrator"]["handler"]
        == "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.HandlerMemoryTick"
    )
    assert (
        routable["node_persona_lifecycle_orchestrator"]["handler"]
        == "omnimarket.nodes.node_persona_lifecycle_orchestrator.handlers.handler_persona_rebuild.HandlerPersonaRebuild"
    )
