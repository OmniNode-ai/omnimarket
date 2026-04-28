# ADR: Agent() Spawn Is a Foreground-Only Capability

## Status
Accepted

## Context
- `node_dispatch_worker` compiles a worker prompt and returns spawn args.
- The actual `Agent(team_name=..., name=...)` call requires the `Agent` tool,
  which is only available in the foreground Claude Code session.
- Subagents (workers spawned by `Agent`), headless `claude -p` print mode,
  Kafka-driven runtime workers, and hook scripts do NOT have access to the
  `Agent` tool. A subagent that compiles spawn args has no way to invoke them
  in-process; the call has to happen in the foreground caller.

## Decision: Two Patterns

### Pattern A — Direct foreground (default)
1. Foreground invokes `uv run onex run-node <backing_node>` synchronously.
2. Node handler compiles prompt + persists `ModelDispatchRecord` + returns
   `ModelDispatchWorkerResult`.
3. Foreground reads `proposed_agent_spawn_args`; calls `TeamCreate` +
   `TaskCreate` + `Agent(**args)`.
4. One Kafka round-trip. Used by all wave 1-4 skills.

### Pattern B — Broker-mediated (DEFERRED)
1. Backing node publishes to `onex.cmd.omnimarket.dispatch-spawn-request.v1`.
2. A persistent foreground broker consumes and calls `Agent()`.
3. Enables subagent-initiated dispatch.
4. Requires `node_skill_dispatch_engine_orchestrator` to graduate from `stub`
   to `production`. Reserved architecture; no live consumer until then.

## Invariants
- No handler, worker, subagent, or hook may call `Agent()`.
- The foreground context is the sole owner of `Agent()`.
- For `dispatch_worker` archetype: foreground reads `proposed_agent_spawn_args`
  and calls `Agent()`. For `deterministic_node` / `review_worker` archetypes:
  no `Agent()` call.
- If a subagent needs to dispatch a worker, it compiles args and publishes to
  the spawn-request topic (Pattern B), then returns. The foreground broker
  performs the spawn.

## Consequences
- Pattern A delivers the entire wave 1-4 migration without Pattern B.
- Pattern B is declared as reserved architecture. The spawn-request topic is
  declared in `contract.yaml` but has no live consumer until the dispatch
  engine orchestrator graduates from `stub` to `production`.
- This ADR explicitly rejects giving handlers direct `Agent` tool access.
