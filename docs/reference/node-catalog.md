# Node Catalog

The canonical entry-point list is
`[project.entry-points."onex.nodes"]` in `pyproject.toml`.

The current repository contains 134 node package directories, exposes 130
runtime entry points, and every direct `src/omnimarket/nodes/node_*` directory
has a `metadata.yaml`.

## Inspect The Catalog

List registered entry points:

```bash
uv run python - <<'PY'
from importlib.metadata import entry_points

for entry_point in sorted(entry_points(group="onex.nodes"), key=lambda item: item.name):
    print(f"{entry_point.name} = {entry_point.value}")
PY
```

List node package directories:

```bash
find src/omnimarket/nodes -mindepth 1 -maxdepth 1 -type d -name 'node_*' | sort
```

Verify every registered entry point has the expected package shape:

```bash
uv run python scripts/ci/run_runtime_sweep.py
```

Current package directories that are intentionally present but not registered as
runtime entry points:

- `node_full_triage_orchestrator`
- `node_overseer_observer`
- `node_routing_policy_engine`
- `node_state_persist_effect`

## Node Families

| Family | Representative nodes |
| --- | --- |
| Build and pipeline | `node_build_loop_orchestrator`, `node_build_dispatch_effect`, `node_loop_state_reducer`, `node_ticket_pipeline`, `node_pipeline_fill`, `node_session_orchestrator` |
| PR and review lifecycle | `node_pr_lifecycle_orchestrator`, `node_pr_lifecycle_triage_compute`, `node_pr_lifecycle_merge_effect`, `node_pr_polish`, `node_pr_review_bot`, `node_rebase_effect`, `node_ci_fix_effect` |
| Validation and diagnostics | `node_platform_readiness`, `node_runtime_sweep`, `node_golden_chain_sweep`, `node_data_flow_sweep`, `node_doc_freshness_sweep`, `node_quality_scoring_compute`, `node_environment_health_scanner` |
| Planning and tickets | `node_design_to_plan`, `node_plan_to_tickets`, `node_create_ticket`, `node_ticket_query`, `node_ticket_work`, `node_rsd_fill_compute` |
| Memory and intelligence | `node_memory_lifecycle_orchestrator`, `node_memory_storage_effect`, `node_intelligence_orchestrator`, `node_intelligence_reducer`, `node_semantic_analyzer_compute`, `node_persona_lifecycle_orchestrator` |
| Projection and data | `node_projection_baselines`, `node_projection_registration`, `node_projection_session_outcome`, `node_projection_query`, `node_log_projection` |
| Operations and integration | `node_emit_daemon`, `node_authorize`, `node_release`, `node_redeploy`, `node_model_router`, `node_onboarding`, `node_monitor_alert_responder` |

## Current Canary Nodes

Use these as reference implementations:

- `node_platform_readiness` for pure compute and readiness results.
- `node_aislop_sweep` for repository-analysis behavior.
- `node_loop_state_reducer` for pure reducer/FSM behavior.
- `node_build_loop_orchestrator` for workflow coordination.
- `node_emit_daemon` for service-node lifecycle.
- `node_projection_*` packages for projection patterns.

## Adding A Catalog Entry

1. Add the node package under `src/omnimarket/nodes/node_<name>/`.
2. Add `contract.yaml` and `metadata.yaml`.
3. Add the entry point to `pyproject.toml`.
4. Add or update a golden-chain test.
5. Run:

```bash
uv run python scripts/ci/run_runtime_sweep.py
uv run python scripts/ci/check_node_metadata_dependencies.py
```
