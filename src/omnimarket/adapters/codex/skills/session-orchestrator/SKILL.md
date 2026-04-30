---
name: session-orchestrator
description: Thin Codex skill shim for node_session_orchestrator. Use to run the session health gate, queue scoring, and dispatch planning loop.
---

# Session Orchestrator

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_session_orchestrator` node. The node owns health gating, queue scoring,
and dispatch planning. Do not add local health probes, dispatch compilation, or
ticket triage logic to this skill.

## Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `correlation_id` | UUID v4 correlation id for the session run | Required |
| `session_id` | Explicit session id. Node auto-generates one when omitted. | Auto |
| `mode` | `interactive` or `autonomous` | `interactive` |
| `dry_run` | Produce plan and receipts without dispatching workers | `false` |
| `skip_health` | Skip Phase 1 health checks | `false` |
| `standing_orders_path` | Standing orders input path | `.onex_state/session/standing_orders.json` |
| `state_dir` | Session state directory | `.onex_state/session` |
| `phase` | Run one phase only (`1`, `2`, `3`) or `0` for full loop | `0` |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "session_orchestrator" \
  --payload '<json-payload>' \
  --timeout-ms 300000
```

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Map user inputs into a JSON payload using the same field names:

- `correlation_id`
- `session_id`
- `mode`
- `dry_run`
- `skip_health`
- `standing_orders_path`
- `state_dir`
- `phase`

Generate a UUIDv4 `correlation_id` when the user does not supply one. Omit
`session_id` only when you want the node to auto-generate it.

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_session_orchestrator/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `session_orchestrator`
- Runtime topic: `onex.cmd.omnimarket.session-orchestrator-start.v1`
- Completion topic: `onex.evt.omnimarket.session-orchestrator-completed.v1`

## Output

Prefer `output_payloads[0]`. Render the node result with `status`,
`halt_reason`, `health_report`, `dispatch_queue`, and `dispatch_receipts`. For
dry runs, report queue length and receipt count without inventing worker
execution details.
