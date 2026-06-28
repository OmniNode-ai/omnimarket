# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for live-path nodes touched by the 2026-06-06 release wave."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"

RELEASE_TOUCHED_NODES = (
    "node_canary_score_reducer",
    "node_code_embedding_effect",
    "node_code_enrichment_effect",
    "node_contract_matcher_compute",
    "node_contract_reducer",
    "node_contract_registry",
    "node_cross_cli_originator",
    "node_decompose_epic_orchestrator",
    "node_delegation_ab_runner",
    "node_deployment_evidence_reducer",
    "node_dirty_canonical_sweep",
    "node_dispatch_queue_drainer",
    "node_monitor_alert_responder",
    "node_e2e_orchestrator",
    "node_evidence_collector_effect",
    "node_evidence_extractor_compute",
    "node_evidence_pipeline_orchestrator",
    "node_evidence_publisher_effect",
    "node_gap_analyzer_compute",
    "node_generate_node_effect",
    "node_generation_consumer",
    "node_handoff_effect",
    "node_knowledge_context_assembler_orchestrator",
    "node_knowledge_context_assembler_reducer",
    "node_linear_updater_effect",
    "node_llm_delegation_projection",
    "node_log_persistence_effect",
    "node_model_comparison_runner",
    "node_model_router",
    "node_multi_agent_orchestrator",
    "node_observability_sink_effect",
    "node_occ_pr_writer_effect",
    "node_omnigate_projection",
    "node_pattern_b_broker",
    "node_pipeline_cache_effect",
    "node_polish_task_classifier",
    "node_projection_cost_by_repo",
    "node_projection_cost_summary",
    "node_projection_cost_token_usage",
    "node_readiness_gate_orchestrator",
    "node_readiness_scorer_compute",
    "node_post_merge_knowledge_sync_orchestrator",
    "node_resume_session_compute",
    "node_rewind_compute",
    "node_routing_policy_engine",
    "node_session_phase_orchestrator",
    "node_session_orchestrator",
    "node_skill_functional_audit_compute",
    "node_swarm_dispatch_orchestrator",
    "node_swarm_fanout_orchestrator",
)


@pytest.mark.unit
@pytest.mark.parametrize("node_name", RELEASE_TOUCHED_NODES)
def test_release_touched_live_path_node_contract_is_parseable(node_name: str) -> None:
    node_dir = NODES_ROOT / node_name
    contract_path = node_dir / "contract.yaml"
    metadata_path = node_dir / "metadata.yaml"

    assert node_dir.is_dir()
    assert contract_path.is_file()
    assert metadata_path.is_file()

    contract = yaml.safe_load(contract_path.read_text())
    metadata = yaml.safe_load(metadata_path.read_text())

    assert isinstance(contract, dict)
    assert isinstance(metadata, dict)
    assert metadata.get("deprecated") is not True
    assert contract.get("name") or contract.get("node_name") or contract.get("id")
    assert (
        contract.get("handler")
        or contract.get("handler_routing")
        or contract.get("subscribe_topics")
        or contract.get("publish_topics")
        or contract.get("topics")
        or contract.get("runtime_dispatch")
    )
