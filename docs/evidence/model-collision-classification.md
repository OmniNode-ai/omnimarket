# Model Collision Classification

Date: 2026-05-16

Scope: duplicate Python model-name collisions from D4, focused on delegation,
routing, and task-contract names. This slice does not change memory DTOs
or intelligence DTOs, and leaves the ModelRoutingDecision and task-contract
model families to their respective owners except as reference rows.

## Classification

| Collision family | Current locations | Bucket | Owner decision |
| --- | --- | --- | --- |
| `ModelDelegationEvent` | `omniclaude/src/omniclaude/delegation/sqlite_adapter.py`; `omnimarket/src/omnimarket/nodes/node_delegation_orchestrator/models/model_delegation_event.py` | canonical-shared for omnimarket wrapper; intentional local for omniclaude SQLite input | Burned down in this slice by making the omnimarket path re-export canonical `omnibase_compat.contracts.delegation.wire.ModelDelegationEventEnvelope`. The omniclaude class is a local persistence input and is no longer cross-repo-colliding after this change. |
| `ModelDispatchRecord` | `omniclaude/src/omniclaude/hooks/model_dispatch_record.py`; `omnimarket/src/omnimarket/nodes/node_dispatch_worker/models/model_dispatch_record.py`; `omnimarket/src/omnimarket/nodes/node_build_dispatch_effect/handlers/dispatch_history_store.py` | rename/consolidate | High-risk dispatch history collision; the shared `ModelDispatchRecord` relocation path is tracked separately. Defer implementation to avoid competing canonical-home decisions. |
| `ModelRoutingDecision` | `omniclaude`, `omnibase_compat`, `omnimarket`, `omnibase_infra` routing/observability paths | rename/consolidate | Reference only. Owning team should decide whether `omnibase_compat.overseer.ModelRoutingDecision` remains canonical or is split by semantic role. |
| `ModelTaskContract`, `ModelMechanicalCheck`, `EnumCheckType` | `omniclaude`, `omnimarket`, `omnibase_core` task-contract/self-check paths | rename/consolidate | Reference only. No task-contract DTO movement in this slice. |

## First Burn-Down Slice

Changed `omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event`
from a local Pydantic class definition into a compatibility import:

`ModelDelegationEvent = omnibase_compat.contracts.delegation.wire.ModelDelegationEventEnvelope`

This keeps existing omnimarket import paths and handler call sites stable while
removing the local class definition that D4 counted as a cross-repo collision.

## D4 Evidence

Baseline scan against canonical `$OMNI_HOME` before applying the worktree
overlay:

```text
D4 status=FAIL finding_count=183
ModelDelegationEvent: ['omniclaude/src/omniclaude/delegation/sqlite_adapter.py', 'omnimarket/src/omnimarket/nodes/node_delegation_orchestrator/models/model_delegation_event.py']
```

Overlay scan with the model-collision worktree replacing canonical
omnimarket in a temporary mini-workspace:

```text
ModelDelegationEvent: []
ModelRoutingDecision: ['omniclaude/src/omniclaude/routing/routing_recorder.py', 'omnibase_compat/src/omnibase_compat/overseer/model_routing_decision.py', 'omnimarket/src/omnimarket/nodes/node_delegation_routing_reducer/models/model_routing_decision.py', 'omnimarket/src/omnimarket/nodes/node_local_supervisor/models/model_local_supervisor_request.py', 'omnibase_infra/src/omnibase_infra/nodes/node_model_router_compute/models/model_routing_decision.py', 'omnibase_infra/src/omnibase_infra/services/observability/agent_actions/models/model_routing_decision.py']
ModelTaskContract: ['omniclaude/src/omniclaude/contracts/task_contract_generator.py', 'omniclaude/src/omniclaude/verification/self_check.py', 'omnimarket/src/omnimarket/nodes/node_session_bootstrap/models/model_task_contract.py']
ModelDispatchRecord: ['omniclaude/src/omniclaude/hooks/model_dispatch_record.py', 'omnimarket/src/omnimarket/nodes/node_build_dispatch_effect/handlers/dispatch_history_store.py', 'omnimarket/src/omnimarket/nodes/node_dispatch_worker/models/model_dispatch_record.py']
D4 status=FAIL finding_count=182
ModelDelegationEvent absent=True
```

Residual D4 failures are expected: `ModelRoutingDecision`, task-contract models,
and `ModelDispatchRecord` each have separate planned shared-model migration work.
