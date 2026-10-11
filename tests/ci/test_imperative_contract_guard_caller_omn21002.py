# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21002: the caller owns the unchanged allowlist and uses core's guard.

The recorded paths come from the guard's omnimarket allowlist at
6adc28168d023d027bf9c1172f262ac03c9d9055. The project locks core 0.47.39,
which predates the ratchet handler, so behavioural probes use an isolated
0.47.41 validator environment without changing the application's lock.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
CALLER = WORKFLOWS / "imperative-contract-guard.yml"
ALLOWLIST = ROOT / "imperative-contract-allowlist.yaml"
CORE_SHA_PREFIX = "031148215"
CORE_USES = "OmniNode-ai/omnibase_core/.github/workflows/imperative-contract-guard.yml"
RECORDED_PATHS = [
    "omnimarket/nodes/node_ab_compare_reducer/handlers/handler_ab_compare_reducer.py",
    "omnimarket/nodes/node_ab_inference_effect/handlers/handler_ab_inference_effect.py",
    "omnimarket/nodes/node_adr_canary_orchestrator/handlers/handler_canary_orchestrator.py",
    "omnimarket/nodes/node_adr_decision_extraction_llm_effect/handlers/handler_decision_extraction.py",
    "omnimarket/nodes/node_adr_document_ingestion_effect/handlers/handler_document_ingestion.py",
    "omnimarket/nodes/node_adr_draft_generation_compute/handlers/handler_adr_generation.py",
    "omnimarket/nodes/node_adr_extraction_grader_llm_effect/handlers/handler_extraction_grader.py",
    "omnimarket/nodes/node_adr_segmentation_llm_effect/handlers/handler_segmentation.py",
    "omnimarket/nodes/node_aislop_sweep/handlers/handler_aislop_sweep.py",
    "omnimarket/nodes/node_architectural_invariant_loop/handlers/handler_architectural_invariant_loop.py",
    "omnimarket/nodes/node_audit_trail_compactor/handlers/handler_audit_trail_compactor.py",
    "omnimarket/nodes/node_auto_merge_effect/handlers/handler_auto_merge_effect.py",
    "omnimarket/nodes/node_baseline_capture/handlers/handler_baseline_capture.py",
    "omnimarket/nodes/node_baseline_capture/handlers/probes/probe_db_row_counts.py",
    "omnimarket/nodes/node_baseline_capture/handlers/probes/probe_git_branches.py",
    "omnimarket/nodes/node_baseline_capture/handlers/probes/probe_github_prs.py",
    "omnimarket/nodes/node_baseline_capture/handlers/probes/probe_kafka_topics.py",
    "omnimarket/nodes/node_baseline_capture/handlers/probes/probe_linear_tickets.py",
    "omnimarket/nodes/node_baseline_capture/handlers/probes/probe_system_health.py",
    "omnimarket/nodes/node_baseline_compare/handlers/handler_baseline_compare.py",
    "omnimarket/nodes/node_budget_policy_compute/handlers/handler_budget_policy.py",
    "omnimarket/nodes/node_build_dispatch_effect/handlers/dispatch_history_store.py",
    "omnimarket/nodes/node_build_loop/handlers/handler_build_loop.py",
    "omnimarket/nodes/node_build_loop_orchestrator/handlers/adapter_delegation_router.py",
    "omnimarket/nodes/node_build_loop_orchestrator/handlers/adapter_linear_fill.py",
    "omnimarket/nodes/node_build_loop_orchestrator/handlers/adapter_llm_classify.py",
    "omnimarket/nodes/node_build_loop_orchestrator/handlers/adapter_llm_dispatch.py",
    "omnimarket/nodes/node_build_loop_orchestrator/handlers/assemble_live.py",
    "omnimarket/nodes/node_build_loop_orchestrator/handlers/model_policy_loader.py",
    "omnimarket/nodes/node_bus_audit_compute/handlers/handler_bus_audit_compute.py",
    "omnimarket/nodes/node_canary_score_reducer/handlers/handler_canary_score_reducer.py",
    "omnimarket/nodes/node_candidate_pool_compute/handlers/handler_candidate_pool.py",
    "omnimarket/nodes/node_chain_diff/handlers/handler_chain_diff.py",
    "omnimarket/nodes/node_chain_diff_compute/handlers/handler_chain_diff.py",
    "omnimarket/nodes/node_ci_fix_effect/handlers/handler_ci_fix.py",
    "omnimarket/nodes/node_ci_rerun_effect/handlers/handler_ci_rerun.py",
    "omnimarket/nodes/node_ci_watch/handlers/handler_ci_watch.py",
    "omnimarket/nodes/node_claim_resolver/handlers/handler_claim_resolver.py",
    "omnimarket/nodes/node_close_out/handlers/handler_close_out.py",
    "omnimarket/nodes/node_closeout_effect/handlers/handler_closeout.py",
    "omnimarket/nodes/node_closeout_verifier_compute/handlers/handler_closeout_verifier.py",
    "omnimarket/nodes/node_code_embedding_effect/handlers/handler_code_embedding_effect.py",
    "omnimarket/nodes/node_code_enrichment_effect/handlers/handler_code_enrichment_effect.py",
    "omnimarket/nodes/node_coderabbit_triage/handlers/handler_coderabbit_triage.py",
    "omnimarket/nodes/node_compliance_sweep/handlers/handler_compliance_sweep.py",
    "omnimarket/nodes/node_conflict_hunk_effect/handlers/handler_conflict_hunk.py",
    "omnimarket/nodes/node_content_ingestion_effect/handlers/handler_content_ingestion.py",
    "omnimarket/nodes/node_context_bundle_generator_compute/handlers/handler_context_bundle.py",
    "omnimarket/nodes/node_context_pack_builder_compute/handlers/handler_context_pack_builder.py",
    "omnimarket/nodes/node_contract_reducer/handlers/handler_contract_reducer.py",
    "omnimarket/nodes/node_contract_registry/handlers/handler_contract_registry.py",
    "omnimarket/nodes/node_contract_sweep/handlers/handler_contract_sweep.py",
    "omnimarket/nodes/node_coverage_sweep/handlers/handler_coverage_sweep.py",
    "omnimarket/nodes/node_data_flow_sweep/handlers/handler_data_flow_sweep.py",
    "omnimarket/nodes/node_delegation_orchestrator/handlers/handler_compliance_loop.py",
    "omnimarket/nodes/node_delegation_quality_gate_reducer/handlers/handler_quality_gate.py",
    "omnimarket/nodes/node_delegation_routing_reducer/handlers/handler_delegation_routing.py",
    "omnimarket/nodes/node_demo_drift_detector/handlers/handler_demo_drift_detector.py",
    "omnimarket/nodes/node_demo_rehearsal/handlers/handler_demo_rehearsal.py",
    "omnimarket/nodes/node_dispatch_worker/handlers/dispatch_record_writer.py",
    "omnimarket/nodes/node_dod_verify/handlers/handler_runtime_sha_verify.py",
    "omnimarket/nodes/node_environment_health_scanner/handlers/prober_containers.py",
    "omnimarket/nodes/node_environment_health_scanner/handlers/prober_emit_daemon.py",
    "omnimarket/nodes/node_environment_health_scanner/handlers/prober_entry_points.py",
    "omnimarket/nodes/node_environment_health_scanner/handlers/prober_hooks.py",
    "omnimarket/nodes/node_environment_health_scanner/handlers/prober_kafka.py",
    "omnimarket/nodes/node_environment_health_scanner/handlers/prober_model_endpoints.py",
    "omnimarket/nodes/node_environment_health_scanner/handlers/prober_projections.py",
    "omnimarket/nodes/node_filesystem_crawler_effect/handlers/handler_filesystem_crawler.py",
    "omnimarket/nodes/node_full_triage_orchestrator/handlers/output_formatter.py",
    "omnimarket/nodes/node_full_triage_orchestrator/handlers/probe_interface.py",
    "omnimarket/nodes/node_integration_test_runner/handlers/handler_integration_test_runner.py",
    "omnimarket/nodes/node_intelligence_reducer/handlers/handler_pattern_lifecycle.py",
    "omnimarket/nodes/node_intelligence_reducer/handlers/handler_process.py",
    "omnimarket/nodes/node_intelligence_reducer/handlers/handler_intelligence_reducer.py",
    "omnimarket/nodes/node_intent_query_effect/handlers/handler_intent_query.py",
    "omnimarket/nodes/node_kafka_topic_emit_probe/handlers/handler_kafka_probe.py",
    "omnimarket/nodes/node_kb_adr_publisher/handlers/handler_kb_adr_publisher.py",
    "omnimarket/nodes/node_kreuzberg_parse_effect/handlers/handler_kreuzberg_parse.py",
    "omnimarket/nodes/node_ledger_append_effect/handlers/handler_ledger_append.py",
    "omnimarket/nodes/node_ledger_hash_compute/handlers/handler_ledger_hash.py",
    "omnimarket/nodes/node_ledger_orchestrator/handlers/handler_ledger_orchestrator.py",
    "omnimarket/nodes/node_ledger_state_reducer/handlers/handler_ledger_state.py",
    "omnimarket/nodes/node_linear_triage/handlers/handler_linear_triage.py",
    "omnimarket/nodes/node_llm_eval_harness/handlers/handler_llm_eval_harness.py",
    "omnimarket/nodes/node_local_review/handlers/handler_local_review.py",
    "omnimarket/nodes/node_local_supervisor/handlers/handler_local_supervisor.py",
    "omnimarket/nodes/node_log_projection/handlers/handler_log_projection.py",
    "omnimarket/nodes/node_memory_lifecycle_orchestrator/handlers/handler_memory_archive.py",
    "omnimarket/nodes/node_memory_lifecycle_orchestrator/handlers/handler_memory_expire.py",
    "omnimarket/nodes/node_memory_lifecycle_orchestrator/handlers/handler_memory_tick.py",
    "omnimarket/nodes/node_merge_effect/handlers/handler_merge.py",
    "omnimarket/nodes/node_merge_sweep_auto_merge_arm_effect/handlers/handler_auto_merge_arm.py",
    "omnimarket/nodes/node_merge_sweep_compute/handlers/handler_merge_sweep.py",
    "omnimarket/nodes/node_merge_sweep_state_reducer/handlers/handler_sweep_state.py",
    "omnimarket/nodes/node_merge_sweep_triage_orchestrator/handlers/handler_triage.py",
    "omnimarket/nodes/node_model_router/handlers/handler_model_router.py",
    "omnimarket/nodes/node_morning_handoff_generator/handlers/handler_morning_handoff_generator.py",
    "omnimarket/nodes/node_navigation_history_reducer/handlers/handler_navigation_history_reducer.py",
    "omnimarket/nodes/node_nightly_loop_controller/handlers/handler_projection_nightly_loop.py",
    "omnimarket/nodes/node_omnigate_check_executor/handlers/handler_check_executor.py",
    "omnimarket/nodes/node_omnigate_receipt_generator/handlers/handler_receipt_generator.py",
    "omnimarket/nodes/node_omnigate_receipt_verifier/handlers/handler_receipt_verifier.py",
    "omnimarket/nodes/node_onboarding/handlers/handler_onboarding.py",
    "omnimarket/nodes/node_output_schema_registry_compute/handlers/handler_output_schema_registry.py",
    "omnimarket/nodes/node_overnight/handlers/handler_overnight.py",
    "omnimarket/nodes/node_overnight/handlers/overseer_tick.py",
    "omnimarket/nodes/node_overseer_benchmarker/handlers/handler_overseer_benchmarker.py",
    "omnimarket/nodes/node_overseer_observer/handlers/handler_evidence_evaluator.py",
    "omnimarket/nodes/node_overseer_observer/handlers/handler_side_effect_observer.py",
    "omnimarket/nodes/node_overseer_verifier/handlers/handler_overseer_verifier.py",
    "omnimarket/nodes/node_overseer_verifier/handlers/handler_overseer_verifier_consumer.py",
    "omnimarket/nodes/node_pattern_b_broker/handlers/adapter_broker_contract_config.py",
    "omnimarket/nodes/node_pattern_b_broker/handlers/adapter_broker_publish.py",
    "omnimarket/nodes/node_pattern_b_broker/handlers/adapter_broker_terminal_consumer.py",
    "omnimarket/nodes/node_persona_lifecycle_orchestrator/handlers/handler_persona_rebuild.py",
    "omnimarket/nodes/node_plan_to_tickets/handlers/handler_plan_to_tickets.py",
    "omnimarket/nodes/node_platform_diagnostics/handlers/dimension_checks.py",
    "omnimarket/nodes/node_platform_diagnostics/handlers/handler_platform_diagnostics.py",
    "omnimarket/nodes/node_platform_readiness/handlers/dimension_checks.py",
    "omnimarket/nodes/node_platform_readiness/handlers/handler_platform_readiness.py",
    "omnimarket/nodes/node_pr_health_monitor/handlers/handler_pr_health_monitor.py",
    "omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/adapter_github_cli.py",
    "omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/adapter_pr_polish_dispatch.py",
    "omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/handler_admin_merge.py",
    "omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/handler_auto_rebase.py",
    "omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/handler_comment_resolution.py",
    "omnimarket/nodes/node_pr_lifecycle_merge_effect/handlers/adapter_github_merge_queue.py",
    "omnimarket/nodes/node_pr_lifecycle_orchestrator/handlers/handler_pr_lifecycle_orchestrator.py",
    "omnimarket/nodes/node_pr_polish/handlers/handler_pr_polish.py",
    "omnimarket/nodes/node_pr_semantic_grader_llm_effect/handlers/handler_pr_semantic_grader.py",
    "omnimarket/nodes/node_pr_snapshot_effect/handlers/handler_pr_snapshot.py",
    "omnimarket/nodes/node_pr_watch_orchestrator/handlers/handler_pr_watch_orchestrator.py",
    "omnimarket/nodes/node_process_watchdog/handlers/check_daemon_health.py",
    "omnimarket/nodes/node_process_watchdog/handlers/check_socket.py",
    "omnimarket/nodes/node_process_watchdog/handlers/checktargets_production.py",
    "omnimarket/nodes/node_process_watchdog/handlers/handler_process_watchdog.py",
    "omnimarket/nodes/node_projection_baselines/handlers/handler_baselines.py",
    "omnimarket/nodes/node_projection_baselines/handlers/handler_projection_baselines.py",
    "omnimarket/nodes/node_projection_cost_by_repo/handlers/handler_projection_cost_by_repo.py",
    "omnimarket/nodes/node_projection_cost_summary/handlers/handler_projection_cost_summary.py",
    "omnimarket/nodes/node_projection_cost_token_usage/handlers/handler_projection_cost_token_usage.py",
    "omnimarket/nodes/node_projection_delegation/handlers/handler_delegation.py",
    "omnimarket/nodes/node_projection_delegation/handlers/handler_projection_delegation.py",
    "omnimarket/nodes/node_projection_dep_health/handlers/handler_projection_dep_health.py",
    "omnimarket/nodes/node_projection_llm_cost/handlers/handler_llm_cost.py",
    "omnimarket/nodes/node_projection_llm_cost/handlers/handler_projection_llm_cost.py",
    "omnimarket/nodes/node_projection_overnight/handlers/handler_projection_overnight.py",
    "omnimarket/nodes/node_projection_registration/handlers/handler_heartbeat_emitter.py",
    "omnimarket/nodes/node_projection_registration/handlers/handler_projection_registration.py",
    "omnimarket/nodes/node_projection_registration/handlers/handler_registration.py",
    "omnimarket/nodes/node_projection_savings/handlers/handler_projection_savings.py",
    "omnimarket/nodes/node_projection_savings/handlers/handler_savings.py",
    "omnimarket/nodes/node_projection_session_outcome/handlers/handler_projection_session_outcome.py",
    "omnimarket/nodes/node_projection_session_outcome/handlers/handler_session_outcome.py",
    "omnimarket/nodes/node_quality_scoring_compute/handlers/enum_onex_strictness_level.py",
    "omnimarket/nodes/node_quality_scoring_compute/handlers/exceptions.py",
    "omnimarket/nodes/node_quality_scoring_compute/handlers/handler_compute.py",
    "omnimarket/nodes/node_quality_scoring_compute/handlers/handler_quality_scoring.py",
    "omnimarket/nodes/node_quality_scoring_compute/handlers/presets.py",
    "omnimarket/nodes/node_quality_scoring_compute/handlers/protocols.py",
    "omnimarket/nodes/node_rebase_effect/handlers/handler_rebase.py",
    "omnimarket/nodes/node_redeploy/handlers/handler_redeploy_kafka.py",
    "omnimarket/nodes/node_retention_cleanup/handlers/handler_retention_cleanup.py",
    "omnimarket/nodes/node_review_thread_reconciler/handlers/handler_review_thread_reconciler.py",
    "omnimarket/nodes/node_routing_policy_engine/handlers/handler_retry_strategy.py",
    "omnimarket/nodes/node_routing_policy_engine/handlers/handler_routing_policy.py",
    "omnimarket/nodes/node_routing_policy_engine/handlers/handler_task_shape_extractor.py",
    "omnimarket/nodes/node_schema_repair_compute/handlers/handler_schema_repair.py",
    "omnimarket/nodes/node_seam_parallel_executor/handlers/handler_seam_parallel_executor.py",
    "omnimarket/nodes/node_session_bootstrap/handlers/handler_session_bootstrap.py",
    "omnimarket/nodes/node_session_compose/handlers/handler_session_compose.py",
    "omnimarket/nodes/node_session_orchestrator/handlers/handler_session_orchestrator.py",
    "omnimarket/nodes/node_session_phase_dispatcher/handlers/handler_session_phase_dispatcher.py",
    "omnimarket/nodes/node_session_phase_evaluator/handlers/handler_session_phase_evaluator.py",
    "omnimarket/nodes/node_session_phase_orchestrator/handlers/handler_session_phase_orchestrator.py",
    "omnimarket/nodes/node_session_post_mortem/handlers/adapter_friction_reader.py",
    "omnimarket/nodes/node_session_post_mortem/handlers/adapter_pr_collector.py",
    "omnimarket/nodes/node_session_post_mortem/handlers/handler_session_post_mortem.py",
    "omnimarket/nodes/node_skill_dispatch_engine_orchestrator/handlers/handler_skill_requested.py",
    "omnimarket/nodes/node_skill_overseer_verify_orchestrator/handlers/handler_skill_requested.py",
    "omnimarket/nodes/node_state_persist_effect/handlers/handler_state_persist_effect.py",
    "omnimarket/nodes/node_sweep_outcome_classify/handlers/handler_outcome_classify.py",
    "omnimarket/nodes/node_test_generator/handlers/handler_test_generator.py",
    "omnimarket/nodes/node_test_generator_compute/handlers/handler_test_generator.py",
    "omnimarket/nodes/node_thread_reply_effect/handlers/handler_thread_reply.py",
    "omnimarket/nodes/node_ticket_pipeline/handlers/handler_report_poster.py",
    "omnimarket/nodes/node_ticket_pipeline/handlers/handler_ticket_pipeline.py",
    "omnimarket/nodes/node_ticket_work/handlers/handler_ticket_work.py",
    "omnimarket/nodes/node_two_strike_arbiter/handlers/handler_two_strike_arbiter.py",
    "omnimarket/nodes/node_verification_receipt_generator/handlers/handler_verification_receipt.py",
    "omnimarket/nodes/node_verified_dispatch_orchestrator/handlers/handler_verified_dispatch_orchestrator.py",
    "omnimarket/nodes/node_verify_effect/handlers/handler_verify.py",
    "omnimarket/nodes/node_version_skew_detector/handlers/handler_version_skew_detector.py",
    "omnimarket/nodes/node_worker_stall_recovery/handlers/handler_stall_recovery.py",
    "omnimarket/nodes/node_swarm_decomposer_compute/handlers/handler_swarm_decomposer.py",
    "omnimarket/nodes/node_swarm_endpoint_health_effect/handlers/handler_swarm_endpoint_health.py",
    "omnimarket/nodes/node_swarm_fanout_effect/handlers/handler_swarm_fanout.py",
    "omnimarket/nodes/node_generate_node_effect/handlers/handler_generate_node.py",
    "omnimarket/nodes/node_log_persistence_effect/handlers/handler_log_persistence_effect.py",
    "omnimarket/nodes/node_observability_sink_effect/handlers/handler_observability_sink_effect.py",
    "omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/adapter_occ_contract.py",
    "omnimarket/nodes/node_routing_policy_engine/handlers/handler_score_lookup.py",
    "omnimarket/nodes/node_ticketing_epic_org_orchestrator/handlers/handler_ticketing_epic_org.py",
    "src/omnimarket/adapters/asyncpg_adapter.py",
    "src/omnimarket/adapters/codex/runtime_client.py",
    "src/omnimarket/adapters/codex/topics.py",
    "src/omnimarket/cli/cli_ab_compare_suite.py",
    "src/omnimarket/cli/commands/trace.py",
    "src/omnimarket/events/topics.py",
    "src/omnimarket/github_api.py",
    "src/omnimarket/github_landing/github_landing_transport.py",
    "src/omnimarket/inference/adapter_inference_bridge.py",
    "src/omnimarket/logging/topics.py",
    "src/omnimarket/nodes/node_delegate_skill_orchestrator/ports/port_direct_curl_dispatch.py",
    "src/omnimarket/nodes/node_data_flow_sweep/lane_target.py",
    "src/omnimarket/nodes/node_emit_daemon/health_probe.py",
    "src/omnimarket/nodes/node_filesystem_crawler_effect/models/model_filesystem_crawler_config.py",
    "src/omnimarket/nodes/node_handoff_effect/service_catalog.py",
    "src/omnimarket/nodes/node_integration_sweep_orchestrator/models/model_integration_sweep_orchestrator_request.py",
    "src/omnimarket/nodes/node_intent_event_consumer_effect/models/model_consumer_config.py",
    "src/omnimarket/nodes/node_kreuzberg_parse_effect/clients/client_kreuzberg.py",
    "src/omnimarket/nodes/node_kreuzberg_parse_effect/models/model_kreuzberg_parse_config.py",
    "src/omnimarket/nodes/node_redeploy/models/model_deployment_adapter.py",
    "src/omnimarket/nodes/node_runner_orchestrator/models/model_runner_result.py",
    "src/omnimarket/projection/api_server.py",
    "src/omnimarket/projection/runner.py",
    "omnimarket/nodes/node_knowledge_context_assembler_orchestrator/handlers/handler_knowledge_context_assembler_orchestrator.py",
    "omnimarket/nodes/node_create_followup_tickets_effect/handlers/handler_create_followup_tickets_effect.py",
]
LIVE_HANDLER = "src/omnimarket/nodes/node_report_validation_compute/handlers/handler_report_validation.py"


def _mapping(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _core(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "--no-project", "--with", "omnibase-core==0.47.41", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
        cwd=ROOT,
    )


def _caller_sha() -> str:
    jobs = _mapping(CALLER)["jobs"]
    assert isinstance(jobs, dict)
    uses = jobs["imperative-contract-guard"]["uses"]
    assert isinstance(uses, str)
    repository, sha = uses.split("@")
    assert repository == CORE_USES
    assert re.fullmatch(r"[0-9a-f]{40}", sha)
    assert sha.startswith(CORE_SHA_PREFIX)
    return sha


def test_caller_uses_core_and_keeps_the_job_and_context() -> None:
    workflow = _mapping(CALLER)
    assert workflow["name"] == "Imperative Contract Guard"
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"imperative-contract-guard"}
    job = jobs["imperative-contract-guard"]
    assert job["uses"] == f"{CORE_USES}@{_caller_sha()}"
    assert job["with"] == {"allowlist-path": ALLOWLIST.name}
    assert workflow["permissions"] == {"contents": "read"}
    for path in WORKFLOWS.iterdir():
        if path.suffix in {".yml", ".yaml"}:
            text = path.read_text(encoding="utf-8")
            assert not re.search(
                r"onex_change_control/\.github/workflows/imperative-contract-guard\.yml",
                text,
            ), path


def test_canonical_shape_hook_and_ci_use_the_exemption_commit() -> None:
    repos = _mapping(ROOT / ".pre-commit-config.yaml")["repos"]
    assert isinstance(repos, list)
    (hook_repo,) = [
        repo
        for repo in repos
        if any(hook["id"] == "canonical-file-shape" for hook in repo["hooks"])
    ]
    assert hook_repo["repo"] == "https://github.com/OmniNode-ai/omnibase_core"
    assert hook_repo["rev"] == _caller_sha()
    jobs = _mapping(WORKFLOWS / "ci.yml")["jobs"]
    assert isinstance(jobs, dict)
    assert jobs["canonical-file-shape"]["env"]["CANONICAL_FILE_SHAPE_CORE"].endswith(
        f"@{_caller_sha()}"
    )


def test_own_allowlist_matches_the_recorded_paths() -> None:
    entries = _mapping(ALLOWLIST)["allowlisted_handlers"]
    assert isinstance(entries, list)
    assert [entry["path"] for entry in entries] == RECORDED_PATHS


def test_core_ratchet_accepts_unchanged_and_shrinking_but_refuses_an_extra_path() -> (
    None
):
    text = ALLOWLIST.read_text(encoding="utf-8")
    entries = _mapping(ALLOWLIST)["allowlisted_handlers"]
    assert isinstance(entries, list)
    extra = "src/omnimarket/nodes/node_planted_effect/handlers/handler_planted.py"
    request = {
        "base": text,
        "head": yaml.safe_dump({"allowlisted_handlers": [*entries, {"path": extra}]}),
        "shrunk": yaml.safe_dump({"allowlisted_handlers": entries[:-1]}),
    }
    result = _core(
        "python",
        "-c",
        "import json, sys; "
        "from omnibase_core.handlers.handler_imperative_allowlist_ratchet import "
        "HandlerImperativeAllowlistRatchet; "
        "from omnibase_core.models.nodes.imperative_contract_guard."
        "model_allowlist_ratchet_input import ModelAllowlistRatchetInput; "
        "p=json.load(sys.stdin); h=HandlerImperativeAllowlistRatchet(); "
        "reports=[h.handle(ModelAllowlistRatchetInput(base_text=p['base'], "
        "head_text=p[k])) for k in ('base', 'shrunk', 'head')]; "
        "print(json.dumps([dict(r.model_dump(), admitted=r.admitted) for r in reports]))",
        stdin=json.dumps(request),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    unchanged, shrunk, planted = json.loads(result.stdout)
    assert unchanged == {
        "added": [],
        "removed": [],
        "bootstrap": False,
        "admitted": True,
    }
    assert shrunk["admitted"] is True
    assert shrunk["removed"] == [RECORDED_PATHS[-1]]
    assert planted["admitted"] is False
    assert planted["bootstrap"] is False
    assert planted["added"] == [extra]


@pytest.mark.parametrize("plant_http", [False, True], ids=["clean", "planted"])
def test_core_guard_accepts_clean_tree_and_refuses_a_planted_live_handler(
    tmp_path: Path,
    plant_http: bool,
) -> None:
    # Use the caller's input; the merge-base caller cannot provide it.
    jobs = _mapping(CALLER)["jobs"]
    assert isinstance(jobs, dict)
    assert jobs["imperative-contract-guard"]["with"]["allowlist-path"] == ALLOWLIST.name
    root = tmp_path / "omnimarket"
    shutil.copytree(
        ROOT / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copyfile(ROOT / "pyproject.toml", root / "pyproject.toml")
    shutil.copyfile(ALLOWLIST, root / ALLOWLIST.name)
    args = (
        "check-imperative-contracts",
        "--repo-root",
        str(root),
        "--allowlist-path",
        ALLOWLIST.name,
        "--scan-freestanding",
        "--json",
    )
    # Each input is a separate bound check so neither scanner run shares the
    # released verifier's 30-second command budget with the other input.
    if not plant_http:
        clean = _core(*args)
        assert clean.returncode == 0, clean.stdout + clean.stderr
        return
    handler = root / LIVE_HANDLER
    original = handler.read_text(encoding="utf-8")
    # The scanner reads this source; it does not execute a network request.
    handler.write_text(
        original + '\nimport httpx\nhttpx.get("http://example.invalid/planted")\n',
        encoding="utf-8",
    )
    planted = _core(*args)
    assert planted.returncode == 1, planted.stdout + planted.stderr
    report = json.loads(planted.stdout)[0]
    assert report["new_violation_count"] > 0
    (violation,) = [
        violation
        for violation in report["new_violations"]
        if violation["handler_path"].endswith("handlers/handler_report_validation.py")
    ]
    assert violation["reachability"] == "live"
    assert "HTTP" in violation["undeclared_transports"]
    assert "undeclared_transport" in violation["violations"]
