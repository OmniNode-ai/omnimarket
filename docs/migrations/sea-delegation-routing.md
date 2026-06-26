# SEA Delegation Routing Migration Boundary

The self-extending-agent (SEA) repo carried a bespoke research-harness escalation
ladder. That capability has a permanent canonical home in OmniMarket's delegation
node surface. This document records the capability-to-canonical-surface routing so
the bespoke source can be deleted safely once its remaining in-repo consumers are
migrated.

## SEA source -> Canonical OmniMarket surface

| SEA module (capability) | Canonical home | Notes |
| --- | --- | --- |
| `delegation/config.py` (tier ladder, budgets) | `configs/routing_tiers.yaml` + task-class overlay `configs/task_class_contracts.v1.yaml` + bifrost endpoint overlay | Tiers, per-tier retry budgets, providers, endpoints, models, and api-key refs are declared in the contract overlay, not built imperatively in Python. Parsed by `node_delegation_routing_reducer.models.model_delegation_config.parse_delegation_config_yaml`. |
| `delegation/executor.py` (escalate / terminate verdict + tier resolution) | `node_delegation_escalation_decision_compute` (`HandlerEscalationDecision`) + `node_delegation_routing_reducer` (`next_eligible_tier` / `tier_max_retries` / `describe_no_higher_tier_available`) | The escalate-or-terminate precedence and the next-eligible-tier resolution. Tier execution itself runs through the canonical HTTP inference effect (`node_llm_delegation_call_effect`), not a shelled CLI. |
| `delegation/events.py` (attempt / escalation / completed events) | Delegation lifecycle topics declared in `node_delegation_orchestrator/contract.yaml` (`delegation-completed`, etc.); escalation events emitted by `handler_delegation_workflow` | Topics are contract-sourced; the orchestrator emits them on the bus. |
| `delegation/validation.py` (generation + readiness gate) | Canonical generation validator invoked by `node_generation_consumer` (`semantic_validation.py`) | Validation logic lives in the validator platform; the generation consumer invokes it. |

## What Market owns

OmniMarket owns the canonical delegation escalation ladder end to end:

| Node | Role | Boundary |
| --- | --- | --- |
| `node_delegation_routing_reducer` | Reducer | Pure routing: maps a request to a tier/model/endpoint/key resolved from the contract overlay. Owns `next_eligible_tier`, `tier_max_retries`, ladder-exhaustion reason. |
| `node_delegation_escalation_decision_compute` | Compute | Pure deterministic escalate-or-terminate verdict (budget, retryability, ladder exhaustion). Zero I/O. |
| `node_delegation_orchestrator` | Orchestrator | Drives the correlation-keyed delegation FSM, resolves config-dependent inputs from the routing reducer, and delegates the verdict to the escalation compute. |
| `node_llm_delegation_call_effect` | Effect | HTTP inference side-effect boundary. The only out-of-process call; no shelled CLI. |

## Swappability and enforcement

Every tier — including the ceiling — resolves its provider, endpoint, model, and
api-key reference from the routing contract deep-merged with the user overlay. A
tier is swapped by editing the overlay (repoint a backend, override its
`endpoint_url` / `model_name` / `secret_ref`): no code change, no environment
variable, no CLI shell-out. Endpoints are never read from environment variables.

## Deletion boundary

The bespoke SEA `delegation/{config,events,executor,validation}.py` modules are
retained in the SEA repo only until their remaining in-repo consumers (the
experiment harness and the regression runner) are migrated to their own canonical
nodes. The escalation-ladder parity that justifies deletion is proven by
`tests/integration/golden_chain/test_sea_delegation_ladder_parity_omn13619.py`,
which asserts each bespoke executor behavior reproduces through the canonical
nodes above. Pruning the bespoke source is the explicit DELETE step of the
migration and runs after every consumer is ported and parity is proven.
