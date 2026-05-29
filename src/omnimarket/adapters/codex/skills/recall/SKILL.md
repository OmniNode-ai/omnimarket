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

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Map user inputs into a JSON payload using the same field names:

- `query`
- `scope`
- `filters`
- `max_results`

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly. A
`node_not_implemented` response is the current explicit stop state, not
permission to call a legacy localhost service.

## Contract

- Backing node: `src/omnimarket/nodes/node_recall_compute/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `recall_compute`
- Runtime topic: `onex.cmd.omnimarket.recall-start.v1`
- Completion topic: `onex.evt.omnimarket.recall-completed.v1`

## Output

Prefer `output_payloads[0]`. Render `confidence`, `sources`, `partial`, and a
ranked result summary. All retrieval behavior is owned by `node_recall_compute`.
