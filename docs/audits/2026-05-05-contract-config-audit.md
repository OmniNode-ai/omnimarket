# Contract Config Audit Summary — 2026-05-05

**Ticket:** OMN-10565 (Task 17, Epic 4: Contract-Declared Config)

**Source CSV:** `docs/audits/2026-05-05-contract-config-audit.csv`

## Counts by classification

| classification | count |
|---|---|
| config_free | 19 |
| config_required | 122 |
| needs_review | 10 |
| **total** | **151** |

## Handler transport tally (across all rows)

| transport | nodes |
|---|---|
| kafka | 5 |
| llm | 1 |
| postgres | 6 |
| valkey | 3 |

## needs_review nodes

These nodes have handler imports that suggest transport usage not declared in `dependencies[]`. Task 18 will leave these flagged for human follow-up rather than auto-editing them.

| node_name | handler_transports | reason |
|---|---|---|
| `node_autopilot_orchestrator` | valkey | handler uses ['valkey'] but contract dependencies[] does not declare it |
| `node_baseline_capture` | postgres,valkey | handler uses ['postgres', 'valkey'] but contract dependencies[] does not declare it |
| `node_compliance_sweep` | postgres | handler uses ['postgres'] but contract dependencies[] does not declare it |
| `node_e2e_orchestrator` | kafka | handler uses ['kafka'] but contract dependencies[] does not declare it |
| `node_environment_health_scanner` | valkey | handler uses ['valkey'] but contract dependencies[] does not declare it |
| `node_intent_query_effect` | postgres | handler uses ['postgres'] but contract dependencies[] does not declare it |
| `node_memory_lifecycle_orchestrator` | postgres | handler uses ['postgres'] but contract dependencies[] does not declare it |
| `node_navigation_history_reducer` | postgres | handler uses ['postgres'] but contract dependencies[] does not declare it |
| `node_pr_review_bot` | llm | handler uses ['llm'] but contract dependencies[] does not declare it |
| `node_projection_llm_cost` | kafka,postgres | handler uses ['postgres'] but contract dependencies[] does not declare it |
