# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression coverage for all-node market runtime dogfood inventory."""

from __future__ import annotations

from scripts.audit.market_node_runtime_dogfood import build_report

FOCUS_NODES = {
    "node_contract_reducer",
    "node_cross_cli_originator",
    "node_e2e_orchestrator",
    "node_finding_aggregator_compute",
    "node_intent_storage_effect",
    "node_llm_delegation_routing_compute",
    "node_memory_storage_effect",
    "node_model_router",
    "node_navigation_history_reducer",
    "node_overseer_observer",
    "node_persona_builder_compute",
    "node_persona_retrieval_effect",
    "node_persona_storage_effect",
    "node_polish_task_classifier",
    "node_projection_dep_health",
    "node_rsd_fill_compute",
    "node_semantic_analyzer_compute",
    "node_similarity_compute",
    "node_ticket_classify_compute",
}

NATIVE_NON_ADDRESSABLE_NODES = {
    "node_e2e_orchestrator",
    "node_navigation_history_reducer",
    "node_projection_dep_health",
}


def test_market_node_runtime_dogfood_inventory_classifies_all_entry_points() -> None:
    report = build_report()
    summary = report["summary"]

    assert summary["node_dirs"] == 296
    assert summary["entry_points"] == 296
    assert summary["missing_entry_points"] == []
    assert summary["dangling_entry_points"] == []
    assert summary["routable"] >= 292
    assert summary["skipped"] == 3
    assert summary["failed"] == 0
    assert summary["failure_buckets"] == {}
    assert {
        item["node_name"]
        for item in report["skipped"]
        if item["bucket"] == "non_addressable"
    } == {
        "node_e2e_orchestrator",
        "node_navigation_history_reducer",
        "node_projection_dep_health",
    }
    assert not {
        item["node_name"]
        for item in report["skipped"]
        if item["node_name"].endswith("_compute")
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
    assert (
        routable["node_emit_daemon"]["input_model"]
        == "omnimarket.nodes.node_emit_daemon.models.model_daemon_state.ModelEmitDaemonCommand"
    )
    assert (
        routable["node_emit_daemon"]["command_topic"]
        == "onex.cmd.omnimarket.emit-daemon-lifecycle.v1"
    )
    assert (
        routable["node_similarity_compute"]["command_topic"]
        == "onex.cmd.omnimemory.similarity-compute.v1"
    )
    assert (
        routable["node_memory_storage_effect"]["command_topic"]
        == "onex.cmd.omnimemory.memory-storage.v1"
    )
    assert (
        routable["node_overseer_observer"]["input_model"]
        == "omnimarket.nodes.node_overseer_observer.models.model_overseer_observation_request.ModelOverseerObservationRequest"
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


def test_market_node_runtime_dogfood_audits_former_non_addressable_nodes() -> None:
    report = build_report()
    routable = {item["node_name"]: item for item in report["routable"]}
    skipped = {item["node_name"]: item for item in report["skipped"]}
    failures = {item["node_name"]: item for item in report["failures"]}

    assert failures.keys().isdisjoint(FOCUS_NODES)
    assert (set(routable) | set(skipped)) & FOCUS_NODES == FOCUS_NODES
    assert set(skipped) & FOCUS_NODES == NATIVE_NON_ADDRESSABLE_NODES

    command_addressable_nodes = FOCUS_NODES - NATIVE_NON_ADDRESSABLE_NODES
    assert command_addressable_nodes <= set(routable)
    assert all(
        routable[node_name]["command_topic"].startswith("onex.cmd.")
        for node_name in command_addressable_nodes
    )
