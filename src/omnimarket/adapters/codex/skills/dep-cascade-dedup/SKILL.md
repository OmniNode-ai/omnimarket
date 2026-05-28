---
name: dep-cascade-dedup
description: Thin Codex skill shim for node_dep_cascade_dedup_orchestrator. Use when deduplicating dependency bump PR cascades through runtime truth.
---

# Dep Cascade Dedup

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_dep_cascade_dedup_orchestrator` node. The node owns PR discovery,
version grouping, close decisions, and GitHub mutations. Do not add GitHub CLI
calls, GitHub API calls, handler imports, or local dedup logic to this skill.

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "dep_cascade_dedup_orchestrator" \
  --payload '<json-payload>' \
  --timeout-ms 600000
```

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Map user inputs into `repos`, `dependency_type`, `label`, `dry_run`, and
`close_comment`. Surface `error.code` and `error.message` directly; do not run
direct GitHub commands when the node returns an explicit not-implemented stop
state.

## Contract

- Backing node: `src/omnimarket/nodes/node_dep_cascade_dedup_orchestrator/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `dep_cascade_dedup_orchestrator`
- Runtime topic: `onex.cmd.omnimarket.dep-cascade-dedup-start.v1`
- Completion topic: `onex.evt.omnimarket.dep-cascade-dedup-completed.v1`
