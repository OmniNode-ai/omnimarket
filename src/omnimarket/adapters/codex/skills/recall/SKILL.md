---
name: recall
description: Thin Codex skill shim for node_recall_compute. Use when querying OmniNode runtime knowledge through the event bus.
---

# Recall

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_recall_compute` node. The node owns backend federation and source
attribution. Do not add localhost HTTP calls, direct search clients, handler
imports, or retrieval fallbacks to this skill.

## Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `query` | Free-text knowledge query | Required |
| `scope` | `learnings`, `architecture`, `antipatterns`, or `all` | `all` |
| `filters` | Optional object containing `repo` and/or `task_type` | Omitted |
| `max_results` | Maximum results per backend | `5` |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "recall_compute" \
  --payload '<json-payload>' \
  --timeout-ms 15000
```

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Map user inputs into `query`, `scope`, `filters`, and `max_results`. Surface
`error.code` and `error.message` directly; do not call a legacy localhost
service when the node returns an explicit not-implemented stop state.

## Contract

- Backing node: `src/omnimarket/nodes/node_recall_compute/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `recall_compute`
- Runtime topic: `onex.cmd.omnimarket.recall-start.v1`
- Completion topic: `onex.evt.omnimarket.recall-completed.v1`
