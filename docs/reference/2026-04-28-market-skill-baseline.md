# Market Skill Baseline

Captured at: `2026-04-29T15:13:01.306814+00:00`
Repo root: `<omnimarket>`

## Summary

- Working: `8`
- Degraded: `0`
- Failing: `0`

## Inventory

| Skill | Node | Contract | CLI smoke | Focused tests | Status |
|-------|------|----------|-----------|---------------|--------|
| aislop_sweep | node_aislop_sweep | aislop_sweep | pass | not-run | working |
| pr_lifecycle_orchestrator | node_pr_lifecycle_orchestrator | pr_lifecycle_orchestrator | pass | not-run | working |
| pr_polish | node_pr_polish | pr_polish | pass | not-run | working |
| local_review | node_local_review | local_review | pass | not-run | working |
| coderabbit_triage | node_coderabbit_triage | coderabbit_triage | pass | not-run | working |
| session_bootstrap | node_session_bootstrap | session_bootstrap | pass | not-run | working |
| session_orchestrator | node_session_orchestrator | session_orchestrator | pass | not-run | working |
| ticket_pipeline | node_ticket_pipeline | ticket_pipeline | pass | not-run | working |

## Details

### aislop_sweep

- Node: `node_aislop_sweep`
- Contract: `aislop_sweep`
- Node type: `compute`
- Timeout: `120000`
- Terminal event: `onex.evt.omnimarket.aislop-sweep-completed.v1`
- Inputs: `target_dirs, checks, dry_run, severity_threshold`
- Contract/model input match: `True`
- CLI smoke status: `pass`
- CLI smoke summary: `{"dry_run": true, "findings_count": 118, "repos_scanned": 1, "status": "findings"}`
- CLI smoke notes: `findings are expected to exit non-zero; this still proves the node ran`

### pr_lifecycle_orchestrator

- Node: `node_pr_lifecycle_orchestrator`
- Contract: `pr_lifecycle_orchestrator`
- Node type: `orchestrator`
- Timeout: `300000`
- Terminal event: `onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1`
- Inputs: `correlation_id, run_id, dry_run, inventory_only, fix_only, merge_only, repos, max_parallel_polish, enable_auto_rebase, use_dag_ordering, enable_trivial_comment_resolution, enable_admin_merge_fallback, admin_fallback_threshold_minutes, verify, verify_timeout_seconds`
- Contract/model input match: `True`
- CLI smoke status: `pass`
- CLI smoke summary: `{"final_state": "COMPLETE", "prs_fixed": 0, "prs_inventoried": 0, "prs_merged": 0, "prs_verified": 0}`

### pr_polish

- Node: `node_pr_polish`
- Contract: `pr_polish`
- Node type: `compute`
- Timeout: `300000`
- Terminal event: `onex.evt.omnimarket.pr-polish-completed.v1`
- Inputs: `repo, pr_number, ticket_id, required_clean_runs, max_iterations, skip_conflicts, skip_pr_review, skip_local_review, no_ci, no_push, no_automerge, dry_run, worktree_path, run_dir`
- Contract/model input match: `True`
- CLI smoke status: `pass`
- CLI smoke summary: `{"error_message": null, "final_phase": "done", "pr_number": 1}`

### local_review

- Node: `node_local_review`
- Contract: `local_review`
- Node type: `compute`
- Timeout: `300000`
- Terminal event: `onex.evt.omnimarket.local-review-completed.v1`
- Inputs: `max_iterations, required_clean_runs, dry_run`
- Contract/model input match: `True`
- CLI smoke status: `pass`
- CLI smoke summary: `{"current_phase": "init", "dry_run": true, "max_iterations": 10, "required_clean_runs": 2}`

### coderabbit_triage

- Node: `node_coderabbit_triage`
- Contract: `coderabbit_triage`
- Node type: `compute`
- Timeout: `120000`
- Terminal event: `onex.evt.omnimarket.coderabbit-triage-completed.v1`
- Inputs: `repo, pr_number, correlation_id, dry_run`
- Contract/model input match: `True`
- CLI smoke status: `pass`
- CLI smoke summary: `{"blocking_count": 0, "dry_run": true, "suggestion_count": 1, "total_threads": 1, "unknown_count": 0}`

### session_bootstrap

- Node: `node_session_bootstrap`
- Contract: `session_bootstrap`
- Node type: `orchestrator`
- Timeout: `30000`
- Terminal event: `onex.evt.omnimarket.session-bootstrap-completed.v2`
- Inputs: `session_id, session_mode, active_sprint_id, model_routing_preference, contract, state_dir, dry_run`
- Contract/model input match: `True`
- CLI smoke status: `pass`
- CLI smoke summary: `{"crons_registered_count": 4, "dry_run": true, "status": "ready"}`

### session_orchestrator

- Node: `node_session_orchestrator`
- Contract: `session_orchestrator`
- Node type: `orchestrator`
- Timeout: `300000`
- Terminal event: `onex.evt.omnimarket.session-orchestrator-completed.v1`
- Inputs: `correlation_id, session_id, mode, dry_run, skip_health, standing_orders_path, state_dir, phase`
- Contract/model input match: `True`
- CLI smoke status: `pass`
- CLI smoke summary: `{"dispatch_queue_count": 0, "dry_run": true, "session_id": "sess-20260429-1513", "status": "complete"}`
- CLI smoke notes: `smoke intentionally bypasses health probes to isolate the market-owned CLI path`
- CLI smoke stderr: `WARNING omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator: skip_health=True — bypassing Phase 1 health gate (emergency only)`

### ticket_pipeline

- Node: `node_ticket_pipeline`
- Contract: `ticket_pipeline`
- Node type: `compute`
- Timeout: `600000`
- Terminal event: `onex.evt.omnimarket.ticket-pipeline-completed.v1`
- Inputs: `ticket_id, skip_test_iterate, dry_run, skip_to`
- Contract/model input match: `True`
- CLI smoke status: `pass`
- CLI smoke summary: `{"phase_results_count": 2, "ran_phase": "implement", "stop_reason": "not_implemented", "stopped_at": "blocked"}`
- CLI smoke notes: `first slice only wires PRE_FLIGHT; IMPLEMENT should block as not_implemented`
