# ADR: Agent() Spawn Is a Foreground-Only Capability

## Status
Accepted (2026-04-27)

## Context
- `node_dispatch_worker` compiles a worker prompt and returns spawn args.
- The actual `Agent(team_name=..., name=...)` call requires the `Agent` tool, which
  is only available in the foreground Claude Code session.
- Subagents (workers spawned by `Agent`), headless `claude -p`, Kafka-driven
  runtime workers, and hook scripts do NOT have access to the `Agent` tool.
- Live evidence 2026-04-27: two `/onex:dispatch_worker` invocations from subagent
  contexts compiled prompts to `/tmp/delegate_topic_fix_prompt.txt` and
  `/tmp/dispatch_prompt.txt` but never spawned. This is a context limitation, not
  a bug — the constraint surfaces in production whenever a subagent attempts to
  dispatch a worker.

## Decision: Two Patterns

### Pattern A — Direct foreground (default)
1. Foreground invokes `uv run onex run-node <backing_node>` synchronously.
2. Node handler compiles prompt + persists `ModelDispatchRecord` + returns
   `ModelDispatchWorkerResult`.
3. Foreground reads `proposed_agent_spawn_args`; calls `TeamCreate` +
   `TaskCreate` + `Agent(**args)`.
4. One Kafka round-trip. Preferred for all wave 1-4 skills.

### Pattern B — Broker-mediated (DEFERRED)
1. Backing node publishes to `onex.cmd.omnimarket.dispatch-spawn-request.v1`.
2. A persistent foreground broker consumes and calls `Agent()`.
3. Enables subagent-initiated dispatch. Not required until that use case exists.
4. Requires `node_skill_dispatch_engine_orchestrator` to graduate from `stub` to
   `production`.
5. Tracked under OMN-NEW-DISPATCH-ENGINE (deferred to Phase 7).

## Invariants
- No handler, worker, subagent, or hook may call `Agent()`.
- The foreground context is the sole owner of `Agent()`.
- For `dispatch_worker` archetype: foreground reads `proposed_agent_spawn_args`
  and calls `Agent()`. For `deterministic_node` / `review_worker` archetypes:
  no `Agent()` call.
- If a subagent needs to dispatch a worker, it compiles args and publishes to
  the spawn-request topic (Pattern B), then returns. The foreground broker
  performs the spawn.
- This ADR makes the "foreground-only Agent() call" invariant from the master
  plan **Current Blocking** once Phase 2 Task 4 (package-boundary validator)
  is wired as CI gate.

## Consequences
- Pattern A delivers the entire wave 1-4 migration without Pattern B.
- Pattern B is declared as reserved architecture. The spawn-request topic is
  declared in `contract.yaml` but has no live consumer until Phase 7.
- This ADR explicitly rejects giving handlers direct `Agent` tool access.
