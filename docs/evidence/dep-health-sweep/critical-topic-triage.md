# CRITICAL Topic Triage — Runtime Ownership Verification

**Date:** 2026-05-15

## Summary

The dependency health sweep found 9 CRITICAL `MISSING_TOPIC_EDGE` findings — command topics
published with no subscriber across 4 scanned repos (omnimarket, omniclaude, omnibase_infra,
omnibase_core). This document triages each finding by searching ALL repos under `$OMNI_HOME`.

**Root cause of false positives:** The sweep handler runs per-repo-root in isolation (`topology = self._topology_parser.parse(search_roots=[repo_root])`). Cross-repo subscribers are invisible to the single-repo topology pass, producing false CRITICAL findings for legitimate cross-repo wiring. The `externally_consumed_topics` annotation suppresses these.

**Before:** 9 CRITICAL | **After:** 0 CRITICAL (sweep returns `"status": "clean"`)

---

## Triage Table

| Topic | Publisher | Subscriber Found | Classification | Fix Applied |
|-------|-----------|-----------------|----------------|-------------|
| `onex.cmd.omnibase-infra.remote-agent-invoke.v1` | `node_delegation_orchestrator` (omnimarket) | YES: `node_remote_agent_invoke_effect` (omnibase_infra) | `intentionally_external` | `externally_consumed_topics` added to publisher contract |
| `onex.cmd.omnibase-infra.delegation-inference-request.v1` | `node_delegation_orchestrator` (omnimarket) | NO — `node_llm_inference_effect` subscribes to different topic (`llm-inference-request`) | `genuinely_unwired` | Allowlisted with expiry 2026-08-15 |
| `onex.cmd.omnibase-infra.delegation-quality-gate-request.v1` | `node_delegation_orchestrator` (omnimarket) | NO — `node_delegation_quality_gate_reducer` exists in omnibase_infra but has no `contract.yaml` | `genuinely_unwired` | Allowlisted with expiry 2026-08-15 |
| `onex.cmd.omnibase-infra.baseline-comparison-request.v1` | `node_delegation_orchestrator` (omnimarket) | NO — `node_baseline_comparison_compute` is COMPUTE-only, no Kafka event bus | `genuinely_unwired` | Allowlisted with expiry 2026-08-15 |
| `onex.cmd.omnibase-infra.consumer-restart.v1` | `node_consumer_health_triage_effect` (omnibase_infra) | NO — runtime-worker uses process-level restart (`util_consumer_restart`), not a Kafka node | `genuinely_unwired` | Allowlisted in omnibase_infra with expiry 2026-08-15 |
| `onex.cmd.omnimarket.alert-fix-requested.v1` | `node_monitor_alert_responder` (omnimarket) | NO — auto-remediation dispatch node not yet implemented | `genuinely_unwired` | Allowlisted with expiry 2026-08-15 |
| `onex.cmd.omnimarket.build-dispatch-effect-start.v1` | `node_swarm_supervisor_orchestrator` (omnimarket) | NO — `node_build_dispatch_effect` subscribes to `build-loop-build.v1` (different topic) | `genuinely_unwired` (topic mismatch) | Allowlisted with expiry 2026-08-15 |
| `onex.cmd.omnimarket.cross-cli-delegation-requested.v1` | `node_cross_cli_originator` (omnimarket, OMN-10143, 2026-05-09) | NO — new node, subscriber not yet wired | `genuinely_unwired` | Allowlisted with expiry 2026-08-15 |
| `onex.cmd.omnimarket.delegation-request.v1` | `node_dispatch_worker_execution_effect`, `node_build_dispatch_effect` (omnimarket) | NO — delegation orchestrators subscribe to `omnibase-infra` prefix, not `omnimarket` prefix | `genuinely_unwired` (prefix inconsistency) | Allowlisted with expiry 2026-08-15 |

---

## Detailed Findings

### Topic 1: `onex.cmd.omnibase-infra.remote-agent-invoke.v1` — RESOLVED

**Publisher:** `node_delegation_orchestrator` (omnimarket)
**Subscriber:** `node_remote_agent_invoke_effect` (omnibase_infra)
**Classification:** `intentionally_external` — valid cross-repo consumer

The subscriber contract at `omnibase_infra/src/omnibase_infra/nodes/node_remote_agent_invoke_effect/contract.yaml` declares:
```yaml
event_bus:
  subscribe_topics:
    - "onex.cmd.omnibase-infra.remote-agent-invoke.v1"
```

The sweep's per-repo isolation causes this to appear as an orphan when scanning omnimarket. Fixed by adding `externally_consumed_topics` to the publisher contract.

**Files changed:**
- `omnimarket/src/omnimarket/nodes/node_delegation_orchestrator/contract.yaml` — added `externally_consumed_topics`

---

### Topic 2: `onex.cmd.omnibase-infra.delegation-inference-request.v1` — ALLOWLISTED

**Publisher:** `node_delegation_orchestrator` (omnimarket)
**Intended subscriber:** `node_llm_inference_effect` (omnibase_infra)
**Why not wired:** `node_llm_inference_effect` subscribes to `onex.cmd.omnibase-infra.llm-inference-request.v1`, a separate general-purpose inference topic. The delegation-specific inference path would require a distinct subscription or topic unification.
**Action needed:** Wire `node_llm_inference_effect` to also subscribe to delegation-inference-request, OR deprecate this topic and route delegation inference through `llm-inference-request`.
**Expires:** 2026-08-15

---

### Topic 3: `onex.cmd.omnibase-infra.delegation-quality-gate-request.v1` — ALLOWLISTED

**Publisher:** `node_delegation_orchestrator` (omnimarket)
**Intended subscriber:** `node_delegation_quality_gate_reducer` (omnibase_infra)
**Why not wired:** `node_delegation_quality_gate_reducer` exists at `omnibase_infra/src/omnibase_infra/nodes/node_delegation_quality_gate_reducer/` but has no `contract.yaml` and therefore no event bus subscription.
**Action needed:** Add `contract.yaml` with `event_bus.subscribe_topics` to `node_delegation_quality_gate_reducer`.
**Expires:** 2026-08-15

---

### Topic 4: `onex.cmd.omnibase-infra.baseline-comparison-request.v1` — ALLOWLISTED

**Publisher:** `node_delegation_orchestrator` (omnimarket)
**Intended subscriber:** `node_baseline_comparison_compute` (omnibase_infra)
**Why not wired:** `node_baseline_comparison_compute` is a COMPUTE node with no `event_bus` section — it operates via direct invocation, not Kafka subscription. The Kafka-triggered path for baseline comparison is not yet implemented.
**Action needed:** Either add a Kafka-subscribed effect wrapper node or remove this topic and use direct invocation only.
**Expires:** 2026-08-15

---

### Topic 5: `onex.cmd.omnibase-infra.consumer-restart.v1` — ALLOWLISTED (omnibase_infra)

**Publisher:** `node_consumer_health_triage_effect` (omnibase_infra)
**Intended subscriber:** Runtime-level consumer watchdog
**Why not wired:** The consumer restart mechanism is implemented at the process level via `util_consumer_restart` (which the observability consumer services import). No ONEX node subscribes to this Kafka command. The runtime-worker service is the intended receiver but doesn't have a contract-declared subscription.
**Action needed:** Implement `node_consumer_restart_effect` as a proper ONEX node that subscribes to this topic and triggers consumer restarts.
**Expires:** 2026-08-15

---

### Topic 6: `onex.cmd.omnimarket.alert-fix-requested.v1` — ALLOWLISTED

**Publisher:** `node_monitor_alert_responder` (omnimarket)
**Intended subscriber:** Auto-remediation dispatch node (not yet created)
**Why not wired:** The monitor alert responder classifies RECOVERABLE alerts and publishes this command, but the node that would receive it and dispatch fix agents doesn't exist yet.
**Action needed:** Implement `node_fix_dispatch_effect` or equivalent consumer.
**Expires:** 2026-08-15

---

### Topic 7: `onex.cmd.omnimarket.build-dispatch-effect-start.v1` — ALLOWLISTED

**Publisher:** `node_swarm_supervisor_orchestrator` (omnimarket)
**Intended subscriber:** `node_build_dispatch_effect` (omnimarket)
**Why not wired:** `node_build_dispatch_effect` subscribes to `onex.cmd.omnimarket.build-loop-build.v1`, not `build-dispatch-effect-start.v1`. There is a topic naming mismatch between what the supervisor publishes and what the build dispatch node subscribes to.
**Action needed:** Align the topic — either update `node_build_dispatch_effect` to subscribe to `build-dispatch-effect-start.v1`, or update `node_swarm_supervisor_orchestrator` to publish `build-loop-build.v1`.
**Expires:** 2026-08-15

---

### Topic 8: `onex.cmd.omnimarket.cross-cli-delegation-requested.v1` — ALLOWLISTED

**Publisher:** `node_cross_cli_originator` (omnimarket, created 2026-05-09, OMN-10143)
**Intended subscriber:** `node_delegation_orchestrator` (omnimarket) or bridge node
**Why not wired:** New node created recently; the delegation orchestrator subscribes to `onex.cmd.omnibase-infra.delegation-request.v1` (omnibase-infra prefix), not the omnimarket-prefixed cross-cli topic.
**Action needed:** Wire `node_delegation_orchestrator` to also subscribe to `cross-cli-delegation-requested.v1`, or create a topic bridge.
**Expires:** 2026-08-15

---

### Topic 9: `onex.cmd.omnimarket.delegation-request.v1` — ALLOWLISTED

**Publisher:** `node_dispatch_worker_execution_effect`, `node_build_dispatch_effect` (omnimarket)
**Intended subscriber:** Delegation orchestration layer
**Why not wired:** There is a service prefix inconsistency. omnimarket nodes publish `onex.cmd.omnimarket.delegation-request.v1` while delegation orchestrators (both in omnimarket and omniclaude) subscribe to `onex.cmd.omnibase-infra.delegation-request.v1`. No node subscribes to the omnimarket-prefixed variant.
**Action needed:** Unify topic prefix (likely standardize to `onex.cmd.omnibase-infra.delegation-request.v1`) or add subscribe declarations for the omnimarket-prefixed variant.
**Expires:** 2026-08-15

---

## Files Changed

### omnimarket (this PR)
- `src/omnimarket/nodes/node_delegation_orchestrator/contract.yaml` — added `externally_consumed_topics` for `remote-agent-invoke.v1`
- `src/dep_health_allowlist.yaml` — created with 7 genuinely-unwired topic entries

### omnibase_infra (separate PR: OMN-11058-infra)
- `src/dep_health_allowlist.yaml` — created with 1 entry for `consumer-restart.v1`

---

## Verification

```text
Before: 9 CRITICAL MISSING_TOPIC_EDGE findings
After:  0 CRITICAL findings (status: clean)

Command:
uv run python -m omnimarket.nodes.node_dependency_health_sweep \
  --repo-roots <OMNI_HOME>/omnimarket/src \
  --repo-roots <OMNI_HOME>/omniclaude/src \
  --repo-roots <OMNI_HOME>/omnibase_infra/src \
  --repo-roots <OMNI_HOME>/omnibase_core/src \
  --severity-threshold CRITICAL --dry-run
Result: {"status": "clean", "findings": [], "summary": {}}
```

---

## Follow-up Tickets Needed

1. Wire `node_delegation_quality_gate_reducer` contract (add contract.yaml)
2. Unify `delegation-request` topic prefix (omnimarket vs omnibase-infra)
3. Implement `node_consumer_restart_effect` or wire runtime-worker as Kafka subscriber
4. Wire `node_build_dispatch_effect` subscribe topic to match supervisor publish topic
5. Wire `node_cross_cli_originator` → delegation orchestrator subscription
