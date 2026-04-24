# Skill, Package, And Node Boundaries

OmniMarket is the canonical owner for portable workflow logic. Wrapper repos are
allowed to expose that logic to humans and agents, but they should not preserve
durable orchestration or business rules in prompt text.

## Ownership Table

| Surface | Owner | Allowed responsibilities | Not allowed |
| --- | --- | --- | --- |
| Platform wrapper | Wrapper repo | Parse user intent, map flags to payload fields, publish command events, monitor completion, render results. | Durable workflow logic, hidden multi-step orchestration, canonical data transforms. |
| Node unit | OmniMarket | Execute one contract boundary with handler logic and tests. | Runtime deployment, host provisioning, platform-specific UX. |
| Workflow package | OmniMarket | Coordinate node units through FSM, routing, reducers, effects, and terminal events. | Concrete host bootstrapping or secret-store ownership. |
| Runtime/infrastructure | Runtime and infrastructure repos | Discover node packages, wire event transports, inject adapters, run services. | Owning portable workflow semantics. |
| Governance | Change-control repo | Policy, readiness contracts, documentation evidence, freshness checks. | Repo-local Market node behavior. |

## Extraction Rule

If a wrapper contains more than argument mapping, command publishing, completion
monitoring, and result formatting, it is likely carrying workflow logic that
belongs in OmniMarket.

Common move candidates:

- Multi-step execution sequences.
- Retry loops with durable state.
- Ticket or pull-request classification.
- Routing decisions.
- LLM model selection.
- Cross-repo readiness checks.
- Data projection or dashboard-visible event production.

Common wrapper-owned behavior:

- Platform-specific invocation syntax.
- Flag parsing and payload assembly.
- Correlation ID creation.
- Timeout and error presentation.
- Output formatting for the target UI.

## Node Roles

OmniMarket nodes typically fall into these roles:

| Role | Responsibility |
| --- | --- |
| Compute | Pure or mostly pure transformation. |
| Reducer | FSM or state transition logic. |
| Effect | Side-effect boundary such as Git, ticket provider, code host, database, or event publisher integration. |
| Orchestrator | Workflow coordination across phases or child nodes. |
| Projection | Event-to-read-model projection. |
| Service | Long-running node with lifecycle and health semantics. |

One node should represent one execution mode transition. Split deterministic
classification, effectful mutation, and long-running orchestration into separate
nodes when that makes testing and dependency boundaries clearer.

## Domain Packages

The long-term package shape groups nodes by domain instead of treating all nodes
as a flat list. Current docs should use these domain labels when they clarify
ownership:

| Domain | Examples |
| --- | --- |
| Build and pipeline | Build loop, ticket pipeline, session orchestration, dispatch. |
| PR lifecycle | Inventory, triage, merge, rebase, review, and CI repair. |
| Validation and diagnostics | Readiness, contract sweep, runtime sweep, data-flow sweep, quality scoring. |
| Planning and tickets | Plan conversion, ticket creation, ticket query, pipeline fill. |
| Memory and intelligence | Memory workflow nodes, intent nodes, semantic analysis, persona lifecycle. |
| Projection and data | Kafka-to-database projections and read-model helpers. |
| Operations | Release, redeploy, authorization, onboarding, model routing, alert response. |

When adding a new node, choose the closest domain and declare it in
`metadata.yaml` with `pack`, `display_name`, `node_role`, and `entry_flags` when
applicable.
