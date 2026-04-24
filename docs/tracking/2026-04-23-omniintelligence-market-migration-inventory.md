# OmniIntelligence To OmniMarket Migration Inventory

## Scope

Worktree: `/Users/jonah/Code/omni_worktrees/OMN-7343/omnimarket`

Ticket anchor:
- `OMN-7343` `Track: omniintelligence handler contract compliance migration`

Related existing work:
- `OMN-7660` is already `Done` and establishes the target architecture: `PluginIntelligence.start_consumers()` should be replaced by contract auto-wiring and lifecycle hooks.
- `OMN-8126` is still active, but it is an integration/gap-closure stream, not the full node migration lane.
- `OMN-8295` is the best reference pattern for repo-to-repo node migration into `omnimarket`.

Open PR check:
- `omniintelligence`: no open PRs
- `omnimarket`: open PRs `#388` and `#383`, both unrelated to intelligence migration

## Current-State Facts

- `omniintelligence` still contains `61` contract-backed node directories.
- `27` of those nodes still declare `event_bus.subscribe_topics`, so they are runtime-active and still participate in the legacy plugin startup path.
- `30` declare `publish_topics`.
- `PluginIntelligence` is still present in `omniintelligence/runtime/plugin.py` and still owns initialization, dispatcher wiring, and consumer startup.
- Only one node is currently duplicated across both repos:
  - `node_quality_scoring_compute`

## Target Boundary

- `omnimarket` owns runnable nodes, contracts, handlers, and entry points.
- `omniintelligence` keeps models, protocols, scoring logic, and other reusable primitives.
- No runtime-owned fallback identity or plugin-owned topic wiring should remain for migrated nodes.
- Configuration must come from typed contract models or typed runtime config models only.

## First Migration Batch

The first batch should target the subscription-bearing nodes that keep the legacy runtime path alive. This is the shortest path to eliminating `PluginIntelligence` as a startup dependency.

Recommended batch 1:
- `node_intelligence_orchestrator`
- `node_intelligence_reducer`
- `node_pattern_projection_effect`
- `node_ci_failure_tracker_effect`
- `node_storage_router_effect`
- `node_routing_feedback_effect`
- `node_llm_routing_decision_effect`
- `node_pattern_storage_effect`
- `node_claude_hook_event_effect`
- `node_bloom_eval_orchestrator`

Why this batch:
- these nodes are all event-bus-active
- they sit close to the runtime-critical intelligence path
- they move orchestration, reduction, and side-effect ownership into `omnimarket`
- they reduce the justification for keeping `PluginIntelligence.start_consumers()` at all

## Secondary Batches

Batch 2:
- remaining subscription-bearing crawler, compliance, pattern-lifecycle, embedding, and protocol nodes

Batch 3:
- pure compute nodes that do not need to stay colocated with runtime/plugin behavior

Batch 4:
- cleanup/tombstone pass in `omniintelligence`
- remove migrated node entry points
- delete legacy plugin wiring paths once no migrated node depends on them

## Immediate Next Actions

1. Canonicalize `node_quality_scoring_compute` so there is only one supported implementation in `omnimarket`.
2. For each batch-1 node, decide what remains in `omniintelligence`:
   - Pydantic models
   - protocols
   - pure helper logic
3. Migrate batch-1 node directories into `omnimarket` with contract-first entry points.
4. Add focused golden-chain or integration proofs for the migrated event chains.
5. Remove the corresponding node entry points and startup wiring from `omniintelligence`.

## Duplicate Node Finding

`node_quality_scoring_compute` is the clearest immediate cleanup target.

Current state:
- both repos still export the node as an `onex.nodes` entry point
- the directory contents are structurally the same
- the `omnimarket` copy is the richer contract surface
- the `omniintelligence` copy is stale and still carries an older event-bus contract section

Conclusion:
- `omnimarket` should be treated as canonical now
- `omniintelligence` should stop exporting and testing its duplicate copy in a dedicated cleanup tranche
- this cleanup is cross-repo because the stale side is still referenced in `pyproject.toml`, `src/omniintelligence/nodes/__init__.py`, and a large node-specific test surface

## Runtime-Active OmniIntelligence Nodes

Nodes with `subscribe_topics` in current `omniintelligence` contracts:

| Node | Type | Subscribe Topics | Publish Topics |
| --- | --- | ---: | ---: |
| `node_intelligence_orchestrator` | `ORCHESTRATOR_GENERIC` | 6 | 10 |
| `node_pattern_projection_effect` | `EFFECT_GENERIC` | 4 | 1 |
| `node_ci_failure_tracker_effect` | `EFFECT_GENERIC` | 3 | 2 |
| `node_storage_router_effect` | `EFFECT_GENERIC` | 3 | 2 |
| `node_claude_hook_event_effect` | `EFFECT_GENERIC` | 2 | 4 |
| `node_crawl_scheduler_effect` | `EFFECT_GENERIC` | 2 | 1 |
| `node_document_fetch_effect` | `EFFECT_GENERIC` | 2 | 1 |
| `node_intelligence_reducer` | `REDUCER_GENERIC` | 2 | 1 |
| `node_llm_routing_decision_effect` | `EFFECT_GENERIC` | 2 | 1 |
| `node_pattern_storage_effect` | `EFFECT_GENERIC` | 2 | 2 |
| `node_routing_feedback_effect` | `EFFECT_GENERIC` | 2 | 2 |
| `node_ast_extraction_compute` | `COMPUTE_GENERIC` | 1 | 1 |
| `node_bloom_eval_orchestrator` | `ORCHESTRATOR` | 1 | 2 |
| `node_code_crawler_effect` | `EFFECT_GENERIC` | 1 | 1 |
| `node_code_entity_bridge_compute` | `COMPUTE_GENERIC` | 1 | 1 |
| `node_compliance_evaluate_effect` | `EFFECT_GENERIC` | 1 | 2 |
| `node_debug_fix_record_effect` | `EFFECT_GENERIC` | 1 | 1 |
| `node_embedding_generation_effect` | `EFFECT_GENERIC` | 1 | 0 |
| `node_enforcement_feedback_effect` | `EFFECT_GENERIC` | 1 | 0 |
| `node_git_repo_crawler_effect` | `EFFECT_GENERIC` | 1 | 3 |
| `node_linear_crawler_effect` | `EFFECT_GENERIC` | 1 | 3 |
| `node_pattern_feedback_effect` | `EFFECT_GENERIC` | 1 | 1 |
| `node_pattern_learning_effect` | `EFFECT_GENERIC` | 1 | 1 |
| `node_pattern_lifecycle_effect` | `EFFECT_GENERIC` | 1 | 1 |
| `node_pattern_promotion_effect` | `EFFECT_GENERIC` | 1 | 2 |
| `node_protocol_handler_effect` | `EFFECT_GENERIC` | 1 | 1 |
| `node_quality_scoring_compute` | `COMPUTE_GENERIC` | 1 | 1 |
